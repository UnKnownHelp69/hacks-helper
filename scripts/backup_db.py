"""Consistent online backup of the SQLite database (safe while the bot is running).

Usage:
    python scripts/backup_db.py [destination]
Default destination: data/backups/bot-YYYYmmdd-HHMMSS.db
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


def main() -> int:
    load_dotenv()
    source = Path(os.getenv("DATABASE_PATH", "data/bot.db"))
    if not source.exists():
        print(f"Database not found: {source}", file=sys.stderr)
        return 1
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = source.parent / "backups" / f"bot-{stamp}.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    print(f"Backup written to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
