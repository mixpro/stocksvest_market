"""Command line: run the service, import a halal DB, or run a sync by hand.

    python -m stocksvest_market serve
    python -m stocksvest_market import-halal ~/Projects/halalterminal_screen.db
    python -m stocksvest_market sync nightly
"""

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    from . import db, sync
    from .alpaca import AlpacaClient
    from .config import Settings

    parser = argparse.ArgumentParser(prog="stocksvest_market")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the API and the background sync")
    serve.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    imp = sub.add_parser("import-halal", help="apply a new halal DB and backfill new symbols")
    imp.add_argument("path", type=Path)
    imp.add_argument("--force", action="store_true", help="allow removing more than half of the symbols")
    run = sub.add_parser("sync", help="run a sync job once")
    run.add_argument("job", choices=["incremental", "nightly"])
    args = parser.parse_args()

    settings = Settings()
    if args.cmd == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return

    conn = db.connect(settings.db_path)
    db.init(conn)
    client = AlpacaClient(settings)
    with sync.WRITE_LOCK:
        if args.cmd == "import-halal":
            result = sync.import_halal(conn, client, settings, args.path.expanduser(), force=args.force)
            if result["added"]:
                if not conn.execute("SELECT 1 FROM calendar LIMIT 1").fetchone():
                    sync.sync_calendar(conn, client, settings, datetime.now(sync.ET).date())
                result["bars"] = sync.sync_all_timeframes(conn, client, settings, datetime.now(timezone.utc))
        elif args.job == "nightly":
            result = sync.run_nightly(conn, client, settings)
        else:
            result = sync.run_incremental(conn, client, settings)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
