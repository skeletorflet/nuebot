from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from nuebot.handlers.buttons import format_caption, parse_params_from_caption
from nuebot.jobs.manager import JobManager, JobParams, apply_result_info
from nuebot.sd.client import SDClient


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

    def test_debug_files_keep_initial_final_payload_and_api_result_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = SDClient.__new__(SDClient)
            client._debug_dir = Path(tmp) / "debug"
            client._write_debug_json("payload_init.json", {"prompt": "cute cat nose", "steps": 4})
            client._write_debug_json("payload_final.json", {"prompt": "cute cat nose", "steps": 4, "seed": -1})
            client._write_debug_json("resultado.json", {"info": "real seed 2418682888", "images": ["png-b64"]})

            debug = client._debug_dir
            self.assertEqual(json.loads((debug / "payload_init.json").read_text(encoding="utf-8"))["steps"], 4)
            self.assertEqual(json.loads((debug / "payload_final.json").read_text(encoding="utf-8"))["seed"], -1)
            self.assertEqual(
                json.loads((debug / "resultado.json").read_text(encoding="utf-8"))["info"],
                "real seed 2418682888",
            )


if __name__ == "__main__":
    unittest.main()
