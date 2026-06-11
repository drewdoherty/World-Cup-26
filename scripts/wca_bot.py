"""Entry point for the World Cup Alpha Telegram management bot.

Usage::

    python scripts/wca_bot.py                 # uses .env + data/wca.db
    python scripts/wca_bot.py --db other.db

Requires in the environment (typically loaded from .env):
    TELEGRAM_BOT_TOKEN   bot token from BotFather
    TELEGRAM_CHAT_ID     your chat id (the only chat allowed to drive the bot)
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from wca.bot.app import run


def _load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader so we don't add a python-dotenv dependency."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description="World Cup Alpha Telegram bot")
    parser.add_argument("--db", default="data/wca.db", help="SQLite ledger path")
    parser.add_argument("--env", default=".env", help="dotenv file to load")
    args = parser.parse_args()

    _load_dotenv(args.env)
    run(db_path=args.db)


if __name__ == "__main__":
    main()
