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
