from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nuebot import config
from nuebot.config import load_generation_settings
from nuebot.sd.client import build_extra_payload, build_hr_block, build_txt2img_payload


class GenerationSettingsTests(unittest.TestCase):
    def test_settings_json_drives_all_static_generation_parameters(self):
        raw = {
            "bot": {"sd_api_url": "http://127.0.0.1:7860", "sd_timeout_s": 600, "allowed_user_id": None},
            "txt2img": {
                "negative_prompt": "bad quality",
                "width": 640,
                "height": 768,
                "steps": 9,
                "cfg_scale": 2.5,
                "sampler_name": "Euler",
                "scheduler": "Normal",
                "seed": -1,
                "batch_size": 1,
                "n_iter": 1,
                "save_images": False,
                "send_images": True,
                "do_not_save_grid": True,
                "custom_flag": "kept",
            },
            "hr": {
                "hr_upscaler": "test-upscaler",
                "hr_scale": 1.5,
                "hr_second_pass_ratio": 0.5,
                "denoising_strength": 0.3,
                "hr_resize_mode": "lanczos",
            },
            "final_upscale": {
                "upscaler_1": "test-upscaler",
                "upscaler_2": "None",
                "extras_upscaler_2_visibility": 0.0,
                "upscale_first": True,
                "resize_mode": 0,
                "show_extras_results": False,
                "upscaling_resize": 3.0,
                "upscaling_crop": False,
                "upscaling_safer": False,
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            settings = load_generation_settings(path)

        payload = build_txt2img_payload(
            prompt="cute cat nose",
            negative_prompt=settings.txt2img.negative_prompt,
            width=settings.txt2img.width,
            height=settings.txt2img.height,
            steps=settings.txt2img.steps,
            cfg_scale=settings.txt2img.cfg_scale,
            sampler=settings.txt2img.sampler_name,
            scheduler=settings.txt2img.scheduler,
            seed=settings.txt2img.seed,
            settings=settings,
        )
        self.assertEqual(payload["custom_flag"], "kept")
        self.assertEqual((payload["width"], payload["height"]), (640, 768))
        self.assertEqual(build_hr_block(steps=9, settings=settings)["hr_second_pass_steps"], 4)
        self.assertEqual(build_extra_payload(image_b64="png", settings=settings)["upscaling_resize"], 3.0)

    def test_named_preset_is_loaded_from_presets_directory(self):
        base = json.loads(Path("settings.json").read_text(encoding="utf-8"))
        preset = {
            key: json.loads(json.dumps(base[key]))
            for key in ("txt2img", "hr", "final_upscale")
        }
        preset["txt2img"]["width"] = 704

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "presets").mkdir()
            (root / "settings.json").write_text(json.dumps(base), encoding="utf-8")
            (root / "presets" / "krea2.json").write_text(json.dumps(preset), encoding="utf-8")
            old_settings = config._settings
            config._settings = None
            try:
                with (
                    patch("nuebot.config.ROOT", root),
                    patch.dict(os.environ, {"NUEBOT_PRESET": "krea2"}),
                ):
                    loaded = load_generation_settings()
            finally:
                config._settings = old_settings

        self.assertEqual(loaded.txt2img.width, 704)
        self.assertEqual(loaded.hr["hr_scale"], 1.25)


if __name__ == "__main__":
    unittest.main()
