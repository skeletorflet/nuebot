"""Cliente async de Stable Diffusion WebUI (A1111 / Forge).

Cubre lo único que usa el bot:
  - POST /sdapi/v1/txt2img           (con o sin enable_hr)
  - POST /sdapi/v1/extra-single-image (Upscale Final)
  - POST /sdapi/v1/interrupt          (cancelar job en GPU)
  - POST /sdapi/v1/options            (aplica preset antes de generar)
  - GET  /sdapi/v1/options            (health)

Notas de quirks del WebUI (jul 2026):
  - txt2img devuelve data["images"] como lista de b64.
  - extra-single-image devuelve data["image"] como único b64 (cuidado singular/plural).
  - POST /options en Forge/Gradio tunnels retorna 200 con body vacío; nunca
    confiar en el POST, re-GET si necesitás confirmar.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class SDError(RuntimeError):
    """Error de comunicación con el WebUI."""


@dataclass
class Txt2ImgResult:
    images_b64: list[str]   # casi siempre 1 elemento
    info_json: dict[str, Any]


_log = logging.getLogger("nuebot.sd")


def _stable_signature(payload: dict[str, Any]) -> tuple:
    """Hash estructural estable: tolera reordenamiento de claves del JSON."""
    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return tuple(sorted((k, normalize(v)) for k, v in value.items()))
        if isinstance(value, list):
            return tuple(normalize(v) for v in value)
        return value
    return normalize(payload)


class SDClient:
    def __init__(self, base_url: str, timeout_s: float, debug_dir: str | Path = "debug") -> None:
        self._base = base_url.rstrip("/")
        self._debug_dir = Path(debug_dir)
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=httpx.Timeout(timeout_s, connect=30.0),
            headers={"Accept": "application/json"},
        )
        # ponytail: cache por hash estructural. Evita re-POST cuando el preset
        # activo ya coincide con el último aplicado, incluso si reordenamos
        # las claves del dict entre runs.
        self._post_options_signature: tuple | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    def _write_debug_json(self, filename: str, data: Any) -> None:
        self._debug_dir.mkdir(parents=True, exist_ok=True)
        (self._debug_dir / filename).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def health(self) -> dict[str, Any]:
        """GET /sdapi/v1/options. Lanza SDError si el endpoint no responde JSON."""
        r = await self._client.get("/sdapi/v1/options")
        r.raise_for_status()
        try:
            return r.json()
        except ValueError as e:
            raise SDError(f"GET /options no devolvió JSON ({r.headers.get('content-type')}): {e}") from e

    async def post_options(self, payload: dict[str, Any] | None) -> None:
        """POST /sdapi/v1/options. Aplica el preset antes de cada generación.

        Idempotente: si el mismo payload ya se posteó, no repite el request.
        No fatal: si el endpoint no responde, loguea y sigue (la generación
        usa el último preset activo del SD).
        """
        if not payload:
            self._post_options_signature = None
            return
        signature = _stable_signature(payload)
        if signature == self._post_options_signature:
            return
        try:
            await self._client.post("/sdapi/v1/options", json=payload, timeout=30.0)
            self._post_options_signature = signature
            _log.info("SD POST /options aplicado: %s", list(payload.keys()))
        except httpx.HTTPError as e:
            _log.warning("No pude aplicar POST /options (%s). Sigo con el preset actual.", e)

    async def txt2img(
        self,
        payload: dict[str, Any],
        *,
        payload_init: dict[str, Any] | None = None,
    ) -> Txt2ImgResult:
        self._write_debug_json("payload_init.json", payload if payload_init is None else payload_init)
        self._write_debug_json("payload_final.json", payload)
        r = await self._client.post("/sdapi/v1/txt2img", json=payload)
        try:
            data = r.json()
        except ValueError:
            data = {"raw_response": r.text}
            self._write_debug_json("resultado.json", data)
            r.raise_for_status()
            raise SDError("txt2img devolvió una respuesta no JSON")
        self._write_debug_json("resultado.json", data)
        r.raise_for_status()
        images = data.get("images") or []
        if not images:
            raise SDError(f"txt2img devolvió 0 imágenes. info={data.get('info', '')[:300]}")
        import json as _json
        info_raw = data.get("info", "{}")
        try:
            info: dict[str, Any] = _json.loads(info_raw) if isinstance(info_raw, str) else (info_raw or {})
        except _json.JSONDecodeError:
            info = {}
        return Txt2ImgResult(images_b64=images, info_json=info)

    async def extra_single_image(self, payload: dict[str, Any]) -> str:
        """POST /sdapi/v1/extra-single-image. Devuelve el b64 de la imagen procesada."""
        self._write_debug_json("payload_init.json", payload)
        self._write_debug_json("payload_final.json", payload)
        r = await self._client.post("/sdapi/v1/extra-single-image", json=payload)
        data = r.json()
        self._write_debug_json("resultado.json", data)
        r.raise_for_status()
        img = data.get("image") or (data.get("images") or [None])[0]
        if not img:
            raise SDError("extra-single-image devolvió respuesta sin imagen")
        return img

    async def interrupt(self) -> None:
        """POST /sdapi/v1/interrupt. Sin body. Fire-and-forget."""
        try:
            await self._client.post("/sdapi/v1/interrupt", timeout=10.0)
        except httpx.HTTPError:
            # Si el SD ya estaba terminando, el interrupt puede tirar. No importa.
            pass


# ---- Payloads ---------------------------------------------------------------

def build_txt2img_payload(
    *,
    prompt: str,
    negative_prompt: str,
    width: int,
    height: int,
    steps: int,
    cfg_scale: float,
    sampler: str,
    scheduler: str,
    seed: int = -1,
    n_iter: int | None = None,
    hr: dict[str, Any] | None = None,
    settings=None,
) -> dict[str, Any]:
    payload: dict[str, Any] = settings.txt2img.model_dump() if settings else {}
    payload["n_iter"] = n_iter if n_iter is not None else payload.get("n_iter", 1)
    payload.update({
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "width": width,
        "height": height,
        "steps": steps,
        "cfg_scale": cfg_scale,
        "sampler_name": sampler,
        "scheduler": scheduler,
        "seed": seed,
    })
    if hr:
        payload["enable_hr"] = True
        payload.update(hr)
    return payload


def build_hr_block(*, steps: int, settings=None) -> dict[str, Any]:
    block = dict(settings.hr) if settings else {
        "hr_upscaler": "4x_NMKD-Superscale-SP_178000_G",
        "hr_scale": 1.5,
        "hr_second_pass_ratio": 0.5,
        "denoising_strength": 0.3,
        "hr_resize_mode": "lanczos",
    }
    ratio = block.pop("hr_second_pass_ratio", 0.5)
    block["hr_second_pass_steps"] = max(1, int(steps * ratio))
    return block


def build_extra_payload(*, image_b64: str, settings=None) -> dict[str, Any]:
    payload = dict(settings.final_upscale) if settings else {
        "upscaler_1": "4x_NMKD-Superscale-SP_178000_G",
        "upscaler_2": "None",
        "extras_upscaler_2_visibility": 0.0,
        "upscale_first": True,
        "resize_mode": 0,
        "show_extras_results": False,
        "upscaling_resize": 3.0,
        "upscaling_crop": False,
        "upscaling_safer": False,
    }
    return {"image": image_b64, **payload}