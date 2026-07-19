from __future__ import annotations

import os
import runpy
import sys
import types
import unittest
from unittest.mock import patch


class StartBotTests(unittest.TestCase):
    def test_token_and_preset_are_set_before_bot_import(self):
        fake_bot = types.ModuleType("nuebot.bot")
        called = []
        fake_bot.run = lambda: called.append((
            os.environ.get("BOT_TOKEN"), os.environ.get("NUEBOT_PRESET")
        ))

        with (
            patch.dict(sys.modules, {"nuebot.bot": fake_bot}),
            patch.object(sys, "argv", [
                "start_bot.py", "cli-token-123456", "--preset", "krea2"
            ]),
            patch.dict(os.environ, {
                "BOT_TOKEN": "env-token-123456", "NUEBOT_PRESET": ""
            }),
        ):
            runpy.run_path("start_bot.py", run_name="__main__")

        self.assertEqual(called, [("cli-token-123456", "krea2")])


if __name__ == "__main__":
    unittest.main()
