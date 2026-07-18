"""Config tipada desde .env. Validación en arranque."""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    bot_token: str = Field(..., min_length=10)
    sd_api_url: str = "http://127.0.0.1:7860"
    sd_timeout_s: float = 600.0
    hr_upscaler: str = "4x_NMKD-Superscale-SP_178000_G"
    final_upscale_factor: float = 3.0
    allowed_user_id: int | None = None

    @field_validator("allowed_user_id", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v):
        # ponytail: .env vacío = "sin restricción". pydantic-settings castea
        # todo a string antes del validator, así que '' se convierte a None acá.
        return None if v == "" else v

    # Defaults txt2img — sanos para el 80% de checkpoints.
    default_width: int = 512
    default_height: int = 512
    default_steps: int = 6
    default_cfg: float = 1.5
    default_sampler: str = "DPM++ 2M SDE"
    default_scheduler: str = "Karras"
    negative_prompt: str = (
        "lowres, bad anatomy, bad hands, text, error, missing fingers, "
        "extra digit, fewer digits, cropped, worst quality, low quality, "
        "normal quality, jpeg artifacts, signature, watermark, username, blurry"
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        if not (ROOT / ".env").exists():
            raise SystemExit(
                f"No existe {ROOT / '.env'}.\n"
                f"Copiá la plantilla:\n"
                f"  cp {ROOT / '.env.example'} {ROOT / '.env'}\n"
                f"Y editá BOT_TOKEN y SD_API_URL."
            )
        _settings = Settings()  # type: ignore[call-arg]
    return _settings
