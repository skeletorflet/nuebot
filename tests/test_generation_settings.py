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


class NoobAiPresetQualityPromptTests(unittest.TestCase):
    """El preset noobai.json es One Obsession v23 (Illustrious-family).
    Debe tener el prompt_prefix canónico y el negative del autor
    (maxfeifei8) — sin esto las generaciones salen planas y con drift
    multi-personaje."""

    def setUp(self):
        import json
        self.preset = json.loads(
            (Path(__file__).resolve().parents[1] / "presets" / "noobai.json").read_text(encoding="utf-8")
        )

    def test_has_prompt_prefix_with_illustrious_quality_stack(self):
        prefix = self.preset["txt2img"].get("prompt_prefix", "")
        for tag in ("masterpiece", "best quality", "amazing quality", "very aesthetic"):
            self.assertIn(tag, prefix, msg=f"prefix Illustrious debe incluir {tag!r}")

    def test_negative_blocks_multi_character_and_anatomy(self):
        neg = self.preset["txt2img"]["negative_prompt"]
        for tag in ("2girls", "multiple girls", "extra person", "bad anatomy",
                     "extra fingers", "watermark", "face backlighting"):
            self.assertIn(tag, neg, msg=f"negative debe bloquear {tag!r}")

    def test_does_not_use_pony_score_block(self):
        # Pony's score_9, score_8_up es contraproducente en Illustrious.
        prefix = self.preset["txt2img"].get("prompt_prefix", "")
        for tag in ("score_9", "score_8_up", "score_7_up"):
            self.assertNotIn(tag, prefix, msg=f"Illustrious no usa {tag!r}")


class PromptPrefixTests(unittest.IsolatedAsyncioTestCase):
    """El preset puede inyectar un prompt_prefix. _enqueue_prompt lo
    prepende al raw antes de expandir wildcards."""

    async def test_prefix_is_prepended_before_wildcards(self):
        from nuebot.handlers.generate import _enqueue_prompt
        from nuebot.config import GenerationSettings, GenerationBlock, BotConfig
        import nuebot.config as cfg_mod

        prefix = "masterpiece, best quality, amazing quality"
        fake_settings = GenerationSettings(
            bot=BotConfig(),
            txt2img=GenerationBlock(negative_prompt="x", steps=20, cfg_scale=1,
                                     sampler_name="Euler a", scheduler="Simple"),
            hr={},
            final_upscale={},
            prompt_prefix=prefix,
        )
        captured = {}

        class _FakeJobs:
            def enqueue(self, job):
                captured["job"] = job
                return 1

        orig = cfg_mod._settings
        cfg_mod._settings = fake_settings
        try:
            await _enqueue_prompt(1, "tres palabras r_female aqui", _FakeJobs(), 1)
        finally:
            cfg_mod._settings = orig

        job = captured["job"]
        self.assertTrue(job.prompt.startswith(prefix + ","))
        # El wildcard se expandió aunque estaba después del prefix
        self.assertNotIn("r_female", job.prompt)

    async def test_no_prefix_keeps_old_behavior(self):
        from nuebot.handlers.generate import _enqueue_prompt
        from nuebot.config import GenerationSettings, GenerationBlock, BotConfig
        import nuebot.config as cfg_mod

        fake_settings = GenerationSettings(
            bot=BotConfig(),
            txt2img=GenerationBlock(negative_prompt="x", steps=20, cfg_scale=1,
                                     sampler_name="Euler a", scheduler="Simple"),
            hr={},
            final_upscale={},
            prompt_prefix=None,
        )
        captured = {}

        class _FakeJobs:
            def enqueue(self, job):
                captured["job"] = job
                return 1

        orig = cfg_mod._settings
        cfg_mod._settings = fake_settings
        try:
            await _enqueue_prompt(1, "tres palabras aqui nomas", _FakeJobs(), 1)
        finally:
            cfg_mod._settings = orig

        self.assertTrue(captured["job"].prompt.startswith("tres palabras"))
