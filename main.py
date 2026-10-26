"""Entry point: python main.py"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from bot.app import run
from bot.config import ConfigError, load_settings
from bot.logging_setup import setup_logging


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        setup_logging()
        logging.getLogger(__name__).error("Configuration error: %s", exc)
        return 2
    setup_logging(settings.log_level, settings.log_file)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
