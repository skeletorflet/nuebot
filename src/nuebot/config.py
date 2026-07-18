"""Config tipada desde settings.json. Validación en arranque."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[2]


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


_settings: GenerationSettings | None = None


def get_settings(path: Path = ROOT / "settings.json") -> GenerationSettings:
    global _settings
    if _settings is None:
        if not path.exists():
            raise SystemExit(
                f"No existe {path}. Copiá la plantilla de settings.json antes de arrancar el bot."
            )
        _settings = GenerationSettings.model_validate_json(path.read_text(encoding="utf-8"))
    return _settings


def load_generation_settings(path: Path = ROOT / "settings.json") -> GenerationSettings:
    return get_settings(path)
