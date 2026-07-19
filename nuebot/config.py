"""Config tipada desde settings.json. Validación en arranque."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]


class GenerationBlock(BaseModel):
    model_config = ConfigDict(extra="allow")

    negative_prompt: str = ""
    width: int = Field(512, ge=64)
    height: int = Field(512, ge=64)
    steps: int = Field(6, ge=1)
    cfg_scale: float = Field(1.5, ge=0)
    sampler_name: str = "DPM++ 2M SDE"
    scheduler: str = "Karras"
    seed: int = -1


class BotConfig(BaseModel):
    sd_api_url: str = "http://127.0.0.1:7860"
    sd_fallback_api_url: str | None = None
    sd_timeout_s: float = 600.0
    allowed_user_id: int | None = None


class GenerationSettings(BaseModel):
    bot: BotConfig
    txt2img: GenerationBlock
    hr: dict[str, Any]
    final_upscale: dict[str, Any]
    # Opcional. Si existe, el cliente SD hace POST /sdapi/v1/options antes de
    # cada generación. Útil para forzar el preset de Forge y los módulos
    # adicionales (VAE, text encoder, etc.) y garantizar que coincidan con el
    # preset activo.
    post_options: dict[str, Any] | None = None


_settings: GenerationSettings | None = None
_PRESET_FIELDS = {
    "txt2img": {
        "negative_prompt", "width", "height", "steps", "cfg_scale",
        "sampler_name", "scheduler", "seed", "batch_size", "n_iter",
        "save_images", "send_images", "do_not_save_grid",
    },
    "hr": {
        "hr_upscaler", "hr_scale", "hr_second_pass_ratio",
        "denoising_strength", "hr_additional_modules",
    },
    "final_upscale": {
        "upscaler_1", "upscaler_2", "extras_upscaler_2_visibility",
        "upscale_first", "resize_mode", "show_extras_results",
        "upscaling_resize", "upscaling_crop", "upscaling_safer",
    },
}


def _read_settings(path: Path) -> GenerationSettings:
    if not path.exists():
        raise SystemExit(f"No existe {path}.")
    try:
        return GenerationSettings.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"Configuración inválida en {path}: {exc}") from exc


def get_settings(path: Path | None = None) -> GenerationSettings:
    global _settings
    if _settings is None:
        _settings = _read_settings(path or ROOT / "settings.json")
    return _settings


def load_generation_settings(path: Path | None = None) -> GenerationSettings:
    if path is not None:
        return _read_settings(path)

    name = os.environ.get("NUEBOT_PRESET")
    if not name:
        return get_settings()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise SystemExit(f"Nombre de preset inválido: {name!r}")

    preset_path = ROOT / "presets" / f"{name}.json"
    if not preset_path.exists():
        raise SystemExit(f"No existe el preset {name!r}: {preset_path}")
    try:
        raw = json.loads(preset_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Preset inválido en {preset_path}: {exc}") from exc

    missing = ["objeto JSON raíz"] if not isinstance(raw, dict) else [
        f"{block}.{field}"
        for block, fields in _PRESET_FIELDS.items()
        for field in sorted(
            fields - set(raw.get(block, {}))
            if isinstance(raw.get(block), dict) else fields
        )
    ]
    if missing:
        raise SystemExit(f"Preset incompleto {name!r}; falta: {', '.join(missing)}")

    raw["bot"] = get_settings().bot.model_dump()
    try:
        return GenerationSettings.model_validate(raw)
    except ValueError as exc:
        raise SystemExit(f"Preset inválido en {preset_path}: {exc}") from exc
