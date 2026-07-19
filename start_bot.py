"""Entry point runner. `python start_bot.py [BOT_TOKEN]` desde la raíz."""
import os
import sys

if len(sys.argv) > 1:
    os.environ["BOT_TOKEN"] = sys.argv[1]

from nuebot.bot import run

if __name__ == "__main__":
    run()
