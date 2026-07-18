from __future__ import annotations

import os
import runpy
import sys
import types
import unittest
from unittest.mock import patch


class StartBotTests(unittest.TestCase):
    def test_positional_token_overrides_env_before_bot_import(self):
        fake_bot = types.ModuleType("nuebot.bot")
        called = []
        fake_bot.run = lambda: called.append(os.environ.get("BOT_TOKEN"))

        with (
            patch.dict(sys.modules, {"nuebot.bot": fake_bot}),
            patch.object(sys, "argv", ["start_bot.py", "cli-token-123456"]),
            patch.dict(os.environ, {"BOT_TOKEN": "env-token-123456"}),
        ):
            runpy.run_path("start_bot.py", run_name="__main__")

        self.assertEqual(called, ["cli-token-123456"])


if __name__ == "__main__":
    unittest.main()
