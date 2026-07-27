"""Editor de progreso que actualiza el mensaje de Telegram en vivo.

Se usa desde los tres lugares donde se genera algo en GPU:
  - txt2img normal (nada de HR)
  - txt2img + HR Upscale (un solo POST con enable_hr=True; dos passes)
  - Final Upscale (txt2img + extra-single-image, dos POSTs)

Diseño:
  - El handler crea el mensaje "Generando..." UNA vez (aparece al toque).
  - Lanza un asyncio.Task con ``ProgressEditor``. Éste hace
    ``edit_message_text`` cada ``interval`` segundos mientras el SD
    reporta que sigue corriendo.
  - El editor termina solo cuando:
      * la coroutine externa (generar) completa -> el editor recibe
        ``generation_done()`` y borra el mensaje de estado en el
        ``finally`` del caller. Eso ya estaba, no cambia.
      * el SD marca ``state.interrupted=True`` -> muestra "❌ cancelado" y
        se detiene solo (no toca el bot del usuario).
      * el bot fue apagado -> shutdown vía ``stop()``.
  - Si el editor no logra contactar al SD un tick (timeout, red), usa el
    último buen valor y sigue. No muestra error en el chat: es la fuente
    "estable" mientras dure el run.
  - Telegram pone un cap de 30 ediciones/minuto por chat en bots; con
    ``interval=3`` durante un run de 30s = ~10 edits, dentro del cap.

Anti-patrones evitados:
  - No hacemos dos endpoints en paralelo (progress + interrupt no).
  - No usamos ``editMessageText`` con preview HTML pesado; sólo texto
    plano con la barra de caracteres unicode.
  - No recreamos el editor por tick: una sola Task por run.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

_log = logging.getLogger("nuebot.progress")

# Caracteres para la barra. Bloques llenos + bloques vacíos.
BAR_FULL = "█"
BAR_EMPTY = "░"
_BAR_LEN = 20

# ponytail: apoptosis threshold. Si job_count>0 y progress no avanza por
# esta cantidad de ticks, asumimos stuck e interrumpimos. 5 × 3s = 15s,
# margen suficiente para que un run real avance varios %.
_STUCK_TICKS = 5


def _bar(percent: int) -> str:
    """Devuelve "████████░░░░░░░░░░░░ 40%" para visualización."""
    percent = max(0, min(100, percent))
    filled = round(percent * _BAR_LEN / 100)
    return BAR_FULL * filled + BAR_EMPTY * (_BAR_LEN - filled)


def _fmt_eta(secs: float) -> str:
    secs = int(max(0, secs))
    if secs >= 60:
        m, s = divmod(secs, 60)
        return f"{m}m {s:02d}s"
    return f"{secs}s"


def _format_progress(
    base_label: str,
    progress: dict,
    *,
    total_steps_label: str | None = None,
) -> str:
    """Mensaje de status compacto, una sola línea + barra de progreso.

    ``progress`` viene de ``SDClient.progress()``; si está vacío
    (timeout, SD ocupado) devolvemos el ``base_label`` solo.
    """
    if not progress:
        return base_label
    p = float(progress.get("progress") or 0.0)
    eta = float(progress.get("eta_relative") or 0.0)
    state = progress.get("state") or {}
    # ``state.sampling_step`` se resetea a 0 entre el primer pass y el HR
    # pass; mostramos el sampling_step sólo cuando hay un run activo
    # (sampling_steps > 0). Si total_steps_label viene del caller
    # (anunciado al usuario) lo anteponemos al detail.
    sample_step = int(state.get("sampling_step") or 0)
    sample_total = int(state.get("sampling_steps") or 0)
    pct = round(p * 100)
    bar = _bar(pct)
    head = f"{base_label}\n{bar} {pct}% · ETA {_fmt_eta(eta)}"
    detail_parts: list[str] = []
    if sample_total > 0:
        # +1 porque el step del backend es 0-indexed en muestreo.
        detail_parts.append(f"step {sample_step + 1}/{sample_total}")
    textinfo = state.get("textinfo") or progress.get("textinfo")
    if isinstance(textinfo, str) and textinfo.strip():
        # Acotamos el textinfo para no romper el cap visual del mensaje.
        short = textinfo.strip().split("\n")[0][:80]
        if short:
            detail_parts.append(short)
    if total_steps_label:
        # Anunciamos el total planificado al usuario (e.g. "28 steps base + 14 HR")
        head = f"{base_label}\n{total_steps_label}\n{bar} {pct}% · ETA {_fmt_eta(eta)}"
    if detail_parts:
        return f"{head}\n" + " · ".join(detail_parts)
    return head


class ProgressEditor:
    """Loop de edición del mensaje de status hasta ``stop()``.

    Uso:
        editor = ProgressEditor(bot, chat_id, status_message_id, label,
                                sd=sd, interval=3.0,
                                total_steps_label="28 base + 14 HR")
        task = asyncio.create_task(editor.run())
        try:
            ... generar ...
        finally:
            await editor.stop()
            # el caller borra el mensaje como siempre.
    """

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        status_message_id: int,
        label: str,
        *,
        sd,                       # SDClient — cualquier objeto con .progress()
        interval: float = 3.0,
        total_steps_label: str | None = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._status_id = status_message_id
        self._label = label
        self._sd = sd
        self._interval = interval
        self._total_steps_label = total_steps_label
        self._stopped = asyncio.Event()
        self._last_text: str = ""
        # Controla el primer poll: si el SD todavía está en job_count == 0
        # (no empezó a generar todavía, p.ej. cargando modelo) no pisamos
        # el "Generando..." inicial en las primeras iteraciones.
        # ponytail: apoptosis. Si el SD reporta job_count>0 pero el percent
        # global no avanza por _STUCK_TICKS ticks seguidos, asumimos que se
        # colgó (modelo swappeando, VRAM OOM, etc) y disparamos interrupt().
        # 5 ticks × 3s = 15s de gracia — un run real siempre avanzó varios %.
        self._last_progress: float = -1.0
        self._stuck_ticks: int = 0
        self._apoptosis_done: bool = False

    def request_stop(self) -> None:
        """Marca para que el loop cierre solo. Idempotente."""
        self._stopped.set()

    async def run(self) -> None:
        """Loop: poll progress, edit_message_text, dormimos ``interval``."""
        # Esperá al menos una vuelta entera con el label inicial antes de
        # empezar a editar; así el primer "Generando..." le llega al user
        # sin una sola edición.
        first_seen_running = False
        while not self._stopped.is_set():
            try:
                progress = await self._sd.progress()
            except Exception as e:  # noqa: BLE001
                # ponytail: si una vez falla la red, no spammeamos logs.
                _log.debug("progress poll falló: %s", e)
                progress = {}
            state = progress.get("state") or {}

            # Si el SD fue interrumpido, mostramos estado "cancelado" y
            # paramos. El caller borra el mensaje después.
            if state.get("interrupted"):
                text = f"{self._label}\n❌ cancelado"
                await self._safe_edit(text)
                self._stopped.set()
                break

            # El SD reporta job_count == 0 cuando está entre runs o
            # cargando modelo. En ese caso conservamos el label original.
            job_count = int(state.get("job_count") or 0)
            if job_count > 0:
                first_seen_running = True

            # ponytail: apoptosis. Si está corriendo y el % global no se mueve
            # por N ticks seguidos, lo matamos y dejamos el texto listo.
            if first_seen_running and not self._apoptosis_done:
                current_progress = float(progress.get("progress") or 0.0)
                if self._last_progress >= 0.0 and current_progress <= self._last_progress:
                    self._stuck_ticks += 1
                else:
                    self._stuck_ticks = 0
                self._last_progress = current_progress
                if self._stuck_ticks >= _STUCK_TICKS:
                    try:
                        await self._sd.interrupt()
                    except Exception as e:  # noqa: BLE001
                        _log.debug("apoptosis interrupt falló: %s", e)
                    await self._safe_edit(f"{self._label}\n❌ sin avance — cancelado")
                    self._apoptosis_done = True
                    self._stopped.set()
                    break

            if first_seen_running:
                text = _format_progress(
                    self._label, progress,
                    total_steps_label=self._total_steps_label,
                )
            else:
                # Primer tick (o pre-arranque): sólo el label, pero ya
                # con un "." extra para que se vea vivo si el SD tarda.
                text = f"{self._label}"

            if text and text != self._last_text:
                await self._safe_edit(text)
                self._last_text = text

            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass

    async def _safe_edit(self, text: str) -> None:
        """edit_message_text tolerante a Telegram rate limits y borrados."""
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._status_id,
                text=text,
            )
        except TelegramAPIError as e:
            # "message is not modified" -> ya tenía ese texto. Silencioso.
            if "not modified" in str(e).lower():
                return
            # Mensaje borrado por el user -> no podemos editarlo.
            # Marcamos stop para no spammear.
            _log.debug("edit progress falló (chat=%s id=%s): %s",
                       self._chat_id, self._status_id, e)
            self._stopped.set()
        except Exception as e:  # noqa: BLE001
            _log.debug("edit progress excepción: %s", e)
