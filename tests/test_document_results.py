from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nuebot.handlers.buttons import format_caption, parse_params_from_caption
from nuebot.jobs.manager import JobManager, JobParams, apply_result_info


class DocumentResultTests(unittest.TestCase):
    def make_params(self, **changes) -> JobParams:
        values = {
            "prompt": "un angel en un bosque",
            "negative_prompt": "lowres, bad anatomy",
            "width": 832,
            "height": 1216,
            "steps": 8,
            "cfg_scale": 1.0,
            "sampler": "Euler a",
            "scheduler": "Normal",
            "seed": -1,
            "author": "Analia",
        }
        values.update(changes)
        return JobParams(**values)

    def test_caption_has_document_style_metadata_and_buttons_can_recover_state(self):
        params = self.make_params(seed=2371856504)
        caption = format_caption("f53b65e1", "txt2img", params)

        self.assertIn("✅ 🎨 Generación completada", caption)
        self.assertIn("📝 Prompt:", caption)
        self.assertIn("⚙️ Configuración:", caption)
        self.assertIn("• Seed: 2371856504", caption)
        self.assertIn("👤 Autor: Analia", caption)
        legacy_caption = (
            "✅ txt2img · f53b65e1 · un angel en un bosque\n"
            "▫ 832×1216 · Euler a · 8 steps · cfg 1.0\n"
            "⚙ p=un angel en un bosque\x1fn=lowres, bad anatomy\x1fw=832\x1fh=1216"
            "\x1fs=8\x1fc=1.0\x1fsm=Euler a\x1fsc=Normal\x1fsd=2371856504\x1fk=txt2img"
        )
        self.assertEqual(parse_params_from_caption(legacy_caption), params)
        self.assertLessEqual(len(caption), 1024)

    def test_result_info_becomes_the_state_used_by_repeat_and_upscale(self):
        original = self.make_params(prompt="payload prompt", negative_prompt="payload negative")
        resolved = apply_result_info(
            original,
            {
                "prompt": "real prompt from INFO",
                "negative_prompt": "real negative from INFO",
                "seed": 2371856504,
                "width": 832,
                "height": 1216,
                "steps": 8,
                "cfg_scale": 1.0,
                "sampler_name": "Euler a",
                "extra_generation_params": {"Schedule type": "Normal"},
            },
        )

        self.assertEqual(resolved.prompt, "real prompt from INFO")
        self.assertEqual(resolved.negative_prompt, "real negative from INFO")
        self.assertEqual(resolved.seed, 2371856504)
        self.assertEqual((resolved.display_width, resolved.display_height), (832, 1216))
        self.assertEqual(resolved.sampler, "Euler a")
        self.assertEqual(resolved.scheduler, "Normal")
        self.assertEqual(resolved.steps, 8)
        self.assertEqual(resolved.cfg_scale, 1.0)
        caption = format_caption("realinfo", "txt2img", resolved)
        self.assertIn("real prompt from INFO", caption)
        self.assertIn("• Scheduler: Normal", caption)
        self.assertNotIn("payload prompt", caption)

    def test_job_params_survive_a_restart_via_disk_cache(self):
        params = self.make_params(seed=2371856504)
        with tempfile.TemporaryDirectory() as tmp:
            first = JobManager(cache_dir=Path(tmp))
            first.remember("f53b65e1", params)

            restarted = JobManager(cache_dir=Path(tmp))
            self.assertEqual(restarted.get_params("f53b65e1"), params)

    def test_long_prompt_keeps_caption_within_telegram_limit(self):
        params = self.make_params(prompt="palabra " * 400, negative_prompt="negativo " * 200)
        caption = format_caption("longtask", "txt2img", params)
        self.assertLessEqual(len(caption), 1024)


if __name__ == "__main__":
    unittest.main()
