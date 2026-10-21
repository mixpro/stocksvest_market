#!/usr/bin/env python3
"""Thin out the off-site backups that tinkergw pushes to the VPS (run daily by cron on the VPS).

Keeps in /opt/stocksvest-market/backups:
  app/YYYY-MM-DD.db         the 30 newest, plus the first backup of each of the 12 newest months
  market/YYYY-MM-DD.db.gz   the 3 newest (it can be rebuilt from Alpaca; old copies add little)

  prune-backups.py [--dry-run]
"""

import re
import sys
from pathlib import Path

ROOT = Path("/opt/stocksvest-market/backups")
DAILY_APP, MONTHLY_APP, DAILY_MARKET = 30, 12, 3  # Used by: keep_app, main
DATED = re.compile(r"^(\d{4}-\d{2}-\d{2})\.")  # backup file names start with their date. Used by: dated


def dated(folder: Path) -> list[tuple[str, Path]]:
    """Backup files of a folder with their date, newest first.

    Args:
        folder: app/ or market/.

    Used by: main.
    """
    out = [(m.group(1), p) for p in folder.glob("*") if (m := DATED.match(p.name))]
    return sorted(out, reverse=True)


def keep_app(files: list[tuple[str, Path]]) -> set[Path]:
    """The app backups to keep: the 30 newest and the first of each of the 12 newest months.

    Args:
        files: (date, path), newest first.

    Used by: main.
    """
    keep = {p for _, p in files[:DAILY_APP]}
    first_of_month: dict[str, Path] = {}
    for day, p in reversed(files):  # oldest first, so the first one of each month wins
        first_of_month.setdefault(day[:7], p)
    for month in sorted(first_of_month, reverse=True)[:MONTHLY_APP]:
        keep.add(first_of_month[month])
    return keep


def main() -> None:
    """Delete what is not kept (or only list it with --dry-run)."""
    dry = "--dry-run" in sys.argv
    app, market = dated(ROOT / "app"), dated(ROOT / "market")
    keep = keep_app(app) | {p for _, p in market[:DAILY_MARKET]}
    for _, p in app + market:
        if p not in keep:
            print(("would delete " if dry else "deleted ") + str(p))
            if not dry:
                p.unlink()


if __name__ == "__main__":
    main()
