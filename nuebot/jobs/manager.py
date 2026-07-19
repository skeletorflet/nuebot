"""FIFO + cancel + cache.

Un solo worker procesa jobs en orden. Los params de cada job exitoso se cachean
para que Repetir / HR Upscale / Upscale Final puedan re-ejecutar con los mismos
ajustes sin pedirle nada al usuario.

El cache vive en RAM durante la ejecución y, cuando se configura ``cache_dir``,
también en JSON para que los botones sigan funcionando después de reiniciar.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any


def new_task_id() -> str:
    """8 chars. Cabe en callback_data junto con el prefijo 'rep:' / 'up:' / 'upx:'."""
    return uuid.uuid4().hex[:8]


@dataclass
class JobParams:
    """Snapshot de los params necesarios para re-ejecutar un job.

    Se cachea cada vez que un job termina OK, key=task_id.
    """
    prompt: str
    negative_prompt: str
    width: int
    height: int
    steps: int
    cfg_scale: float
    sampler: str
    scheduler: str
    seed: int
    # Valores confirmados por la respuesta de SD y mostrados en el documento.
    display_width: int | None = None
    display_height: int | None = None
    author: str = "Analia"
    # Para HR Upscale: marca si el origen es un txt2img (con HR permitido)
    # o un upscaled final (sin HR, solo repite).
    kind: str = "txt2img"   # "txt2img" | "hires" | "final"


def _result_value(info: dict[str, Any], *keys: str) -> Any:
    """Obtiene un valor de INFO aun cuando Forge/A1111 cambia el nombre."""
    for key in keys:
        value = info.get(key)
        if value not in (None, ""):
            return value
    return None


def _info_scheduler(info: dict[str, Any]) -> Any:
    scheduler = _result_value(info, "scheduler", "schedule_type")
    if scheduler is not None:
        return scheduler
    extra = info.get("extra_generation_params")
    if isinstance(extra, dict):
        return extra.get("Schedule type") or extra.get("Scheduler")
    return None


def apply_result_info(params: JobParams, info: dict[str, Any] | None) -> JobParams:
    """Usa INFO como fuente de verdad para caption y acciones posteriores."""
    if not info:
        return params
    seed = _result_value(info, "seed", "all_seeds")
    if isinstance(seed, list):
        seed = seed[0] if seed else None
    width = _result_value(info, "width", "W")
    height = _result_value(info, "height", "H")
    steps = _result_value(info, "steps", "steps_count", "sampling_steps")
    cfg_scale = _result_value(info, "cfg_scale", "cfg")
    sampler = _result_value(info, "sampler_name", "sampler")
    scheduler = _info_scheduler(info)
    updates: dict[str, Any] = {}
    for name, value, converter in (
        ("seed", seed, int),
        ("display_width", width, int),
        ("display_height", height, int),
        ("steps", steps, int),
        ("cfg_scale", cfg_scale, float),
    ):
        if value is not None:
            try:
                updates[name] = converter(value)
            except (TypeError, ValueError):
                pass
    if sampler is not None:
        updates["sampler"] = str(sampler)
    if scheduler is not None:
        updates["scheduler"] = str(scheduler)
    for name, aliases in (("prompt", ("prompt", "all_prompts")), ("negative_prompt", ("negative_prompt", "all_negative_prompts"))):
        value = _result_value(info, *aliases)
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, str):
            updates[name] = value
    if width is not None:
        try:
            updates["width"] = int(width)
        except (TypeError, ValueError):
            pass
    if height is not None:
        try:
            updates["height"] = int(height)
        except (TypeError, ValueError):
            pass
    return replace(params, **updates)


@dataclass
class Job:
    task_id: str
    chat_id: int
    prompt: str
    # Si repite desde botón, ya viene con los params exactos.
    params: JobParams | None = None
    # Mensaje "🎨 Generando..." original, para borrarlo al terminar.
    status_message_id: int | None = None
    # Mensaje del usuario con el prompt original (para borrarlo si falla).
    user_message_id: int | None = None
    # Prompt crudo del usuario (pre-resource-expansion). Para REINTENTAR tras falla.
    raw_prompt: str | None = None
    # Lo que tenía que hacer (txt2img | hires | final). Solo txt2img se encola
    # desde el usuario; los demás los dispara el handler de botones directamente.
    kind: str = "txt2img"
    future: asyncio.Future = field(default=None)  # type: ignore[assignment]

    def short(self) -> str:
        return f"#{self.task_id} · {self.kind} · {self.prompt[:40]}"


class JobManager:
    """Cola + cache. Un solo worker."""

    def __init__(self, max_cache: int = 256, cache_dir: Path | None = None) -> None:
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._pending: dict[str, Job] = {}     # por task_id, los que aún no arrancó el worker
        self._current: Job | None = None       # el que está corriendo ahora
        self._cache: OrderedDict[str, JobParams] = OrderedDict()
        self._max_cache = max_cache
        self._cache_dir = cache_dir
        # ponytail: raw_prompt del usuario sobrevive al fallo hasta que consuma REINTENTAR o hasta N entradas (FIFO).
        self._retries: OrderedDict[str, str] = OrderedDict()
        self._max_retries = 64
        if self._cache_dir is not None:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ---- Cola ---------------------------------------------------------------

    def enqueue(self, job: Job) -> int:
        """Encola un job. Devuelve la posición en la cola (1-based)."""
        job.future = asyncio.get_event_loop().create_future()
        self._pending[job.task_id] = job
        self._queue.put_nowait(job)
        return len(self._pending)

    async def wait_done(self, task_id: str) -> None:
        """Await al future del job. Lanza CancelledError si fue cancelado.

        Tolerante: si el job ya no existe (porque terminó o nunca estuvo),
        retorna sin error.
        """
        job = self._pending.get(task_id) or self._current
        if job is None or job.future is None:
            return
        await job.future

    def current_task_id(self) -> str | None:
        return self._current.task_id if self._current else None

    def pending_ids(self) -> list[str]:
        return list(self._pending.keys())

    def store_retry(self, task_id: str, raw_prompt: str) -> None:
        self._retries[task_id] = raw_prompt
        self._retries.move_to_end(task_id)
        while len(self._retries) > self._max_retries:
            self._retries.popitem(last=False)

    def pop_retry(self, task_id: str) -> str | None:
        return self._retries.pop(task_id, None)

    def set_status_message(self, task_id: str, message_id: int) -> None:
        """Adjunta el id del mensaje de estado ("Generando...") a un job pendiente."""
        job = self._pending.get(task_id) or self._current
        if job is not None:
            job.status_message_id = message_id

    # ---- Worker loop --------------------------------------------------------

    async def worker_loop(self, handler) -> None:
        """Llama a `handler(job)` por cada job. El handler es el que sabe hablar
        con el bot (enviar fotos, mensajes, etc.) y con el SDClient."""
        while True:
            job = await self._queue.get()
            # Saco de pending; ahora soy el current.
            self._pending.pop(job.task_id, None)
            self._current = job
            try:
                await handler(job)
                if not job.future.done():
                    job.future.set_result(None)
            except asyncio.CancelledError:
                if not job.future.done():
                    job.future.set_exception(asyncio.CancelledError("cancelado"))
                # Re-raise para que el loop no se silencie
                raise
            except Exception as e:  # noqa: BLE001
                if not job.future.done():
                    job.future.set_exception(e)
                # No re-raise: el worker sigue con el siguiente job.
            finally:
                self._current = None
                self._queue.task_done()

    # ---- Cancel -------------------------------------------------------------

    async def cancel(self, task_id: str, interrupt_fn) -> str:
        """Cancela un job por task_id. interrupt_fn() se llama si es el current.

        Para jobs en cola: marca _cancelled y setea el future con excepción.
        El handler chequea _cancelled al arrancar y retorna de inmediato
        sin gastar GPU.

        Devuelve un mensaje corto para mostrarle al usuario.
        """
        if self._current and self._current.task_id == task_id:
            await interrupt_fn()
            if self._current.future and not self._current.future.done():
                self._current.future.set_exception(asyncio.CancelledError("cancelado por usuario"))
            return f"❌ Cancelé el job actual {task_id}."

        job = self._pending.pop(task_id, None)
        if job is not None:
            job._cancelled = True  # type: ignore[attr-defined]
            if job.future and not job.future.done():
                job.future.set_exception(asyncio.CancelledError("cancelado en cola"))
            return f"❌ Saqué {task_id} de la cola (no había arrancado)."

        # Ni current ni pending: o ya terminó, o está arrancando AHORA
        # (ventana entre _queue.get() y _current = job). Avisamos al user y
        # dejamos que el handler (que ya arrancó) termine. No perdemos GPU
        # porque el job ya está en vuelo — solo evitamos perder otra corrida.
        return f"⚠️ {task_id} ya está arrancando; esperá que termine."

    # ---- Cache --------------------------------------------------------------

    def remember(self, task_id: str, params: JobParams) -> None:
        self._cache[task_id] = params
        self._cache.move_to_end(task_id)
        while len(self._cache) > self._max_cache:
            self._cache.popitem(last=False)
        if self._cache_dir is not None:
            path = self._cache_dir / f"{task_id}.json"
            path.write_text(json.dumps(asdict(params), ensure_ascii=False), encoding="utf-8")

    def get_params(self, task_id: str) -> JobParams | None:
        cached = self._cache.get(task_id)
        if cached is not None:
            return cached
        if self._cache_dir is None:
            return None
        path = self._cache_dir / f"{task_id}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            params = JobParams(**raw)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None
        self._cache[task_id] = params
        self._cache.move_to_end(task_id)
        return params
