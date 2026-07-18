from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nuebot.handlers.generate import expand_resource_tokens


class ResourcePromptTests(unittest.TestCase):
    def test_resource_token_becomes_four_unique_dynamic_prompt_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            resources = Path(tmp)
            lines = ["alpha", "beta", "gamma", "delta", "epsilon"]
            (resources / "f_anime.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

            with patch.object(random, "sample", return_value=["alpha", "beta", "gamma", "delta"]):
                result = expand_resource_tokens("portrait f_anime cinematic", resources)

            self.assertEqual(result, "portrait {alpha|beta|gamma|delta} cinematic")


if __name__ == "__main__":
    unittest.main()
