"""Cliente async de Stable Diffusion WebUI (A1111 / Forge).

Cubre lo único que usa el bot:
  - POST /sdapi/v1/txt2img           (con o sin enable_hr)
  - POST /sdapi/v1/extra-single-image (Upscale Final)
  - POST /sdapi/v1/interrupt          (cancelar job en GPU)
  - GET  /sdapi/v1/options            (health)

Notas de quirks del WebUI (jul 2026):
  - txt2img devuelve data["images"] como lista de b64.
  - extra-single-image devuelve data["image"] como único b64 (cuidado singular/plural).
  - POST /options en Forge/Gradio tunnels retorna 200 con body vacío; nunca
    confiar en el POST, re-GET si necesitás confirmar.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class SDError(RuntimeError):
    """Error de comunicación con el WebUI."""


@dataclass
class Txt2ImgResult:
    images_b64: list[str]   # casi siempre 1 elemento
    info_json: dict[str, Any]


class SDClient:
    def __init__(self, base_url: str, timeout_s: float) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=httpx.Timeout(timeout_s, connect=30.0),
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict[str, Any]:
        """GET /sdapi/v1/options. Lanza SDError si el endpoint no responde JSON."""
        r = await self._client.get("/sdapi/v1/options")
        r.raise_for_status()
        try:
            return r.json()
        except ValueError as e:
            raise SDError(f"GET /options no devolvió JSON ({r.headers.get('content-type')}): {e}") from e

    async def txt2img(self, payload: dict[str, Any]) -> Txt2ImgResult:
        r = await self._client.post("/sdapi/v1/txt2img", json=payload)
        r.raise_for_status()
        data = r.json()
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
        r = await self._client.post("/sdapi/v1/extra-single-image", json=payload)
        r.raise_for_status()
        data = r.json()
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
    hr: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "width": width,
        "height": height,
        "steps": steps,
        "cfg_scale": cfg_scale,
        "sampler_name": sampler,
        "scheduler": scheduler,
        "seed": seed,
        "batch_size": 1,
        "n_iter": 1,
        "save_images": False,
        "send_images": True,
        "do_not_save_grid": True,
    }
    if hr:
        payload["enable_hr"] = True
        payload.update(hr)
    return payload


def build_hr_block(*, upscaler: str, steps: int) -> dict[str, Any]:
    # ponytail: second pass = steps // 2 (no 4 hardcoded, no 0). Si steps=8 → 4,
    # steps=20 → 10, steps=25 → 12. El caller pasa steps del JobParams.
    return {
        "hr_upscaler": upscaler,
        "hr_scale": 1.5,
        "hr_second_pass_steps": steps // 2,
        "denoising_strength": 0.3,
        "hr_resize_mode": "lanczos",
    }


def build_extra_payload(*, image_b64: str, upscaler: str, resize_factor: float) -> dict[str, Any]:
    return {
        "image": image_b64,
        "upscaler_1": upscaler,
        "upscaler_2": "None",
        "extras_upscaler_2_visibility": 0.0,
        "upscale_first": True,
        "resize_mode": 0,            # 0 = just resize
        "show_extras_results": False,
        "upscaling_resize": resize_factor,
        "upscaling_crop": False,
        "upscaling_safer": False,
    }
