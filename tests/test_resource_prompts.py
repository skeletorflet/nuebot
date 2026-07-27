from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nuebot.handlers.generate import expand_resource_tokens


class ResourcePromptTests(unittest.TestCase):
    def test_resource_token_becomes_up_to_twelve_unique_dynamic_prompt_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            resources = Path(tmp)
            lines = [f"option-{index}" for index in range(13)]
            (resources / "f_anime.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

            with patch.object(random, "sample", return_value=lines[:12]) as sample:
                result = expand_resource_tokens("portrait f_anime cinematic", resources)

            self.assertEqual(result, "portrait {" + "|".join(lines[:12]) + "} cinematic")
            sample.assert_called_once_with(lines, 12)


if __name__ == "__main__":
    unittest.main()


class PromptCapTests(unittest.IsolatedAsyncioTestCase):
    """El cap MAX_PROMPT_BYTES evita expansión DoS antes de encolar."""

    async def test_enqueue_raises_when_expanded_prompt_exceeds_cap(self):
        from nuebot.handlers.generate import _enqueue_prompt, MAX_PROMPT_BYTES

        class _FakeJobs:
            def enqueue(self, job):  # pragma: no cover - shouldn't be called
                raise AssertionError("enqueue no debería dispararse si el cap rechaza")

        huge = "x " * (MAX_PROMPT_BYTES // 2 + 100)  # >> cap
        with self.assertRaises(ValueError) as ctx:
            await _enqueue_prompt(1, huge, _FakeJobs(), 1)  # type: ignore[arg-type]
        self.assertIn("demasiado largo", str(ctx.exception))

    async def test_enqueue_accepts_normal_prompt(self):
        from nuebot.handlers.generate import _enqueue_prompt

        captured = {}

        class _FakeJobs:
            def enqueue(self, job):
                captured["job"] = job
                return 1

        await _enqueue_prompt(1, "tres palabras simples aqui", _FakeJobs(), 1)  # type: ignore[arg-type]
        self.assertIn("job", captured)
