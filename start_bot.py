"""Entry point runner. `python start_bot.py [BOT_TOKEN] [--preset NOMBRE]`."""
import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bot_token", nargs="?")
    parser.add_argument("--preset")
    args = parser.parse_args()

    if args.bot_token:
        os.environ["BOT_TOKEN"] = args.bot_token
    if args.preset:
        os.environ["NUEBOT_PRESET"] = args.preset

    # El preset y el token deben existir antes de importar config/bot.
    from nuebot.bot import run
    run()

if __name__ == "__main__":
    main()
