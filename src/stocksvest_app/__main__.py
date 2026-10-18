"""Run the app service: python -m stocksvest_app serve"""

import argparse
import logging
import os
from pathlib import Path


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader; real environment variables win.

    Args:
        path: the .env file; ignored when missing. Lines are KEY=VALUE; quotes around values are removed.

    Used by: main.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main() -> None:
    """Command line entry point: `serve [--host H] [--port P]` runs the API, sample UI and scheduler."""
    load_dotenv()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    parser = argparse.ArgumentParser(prog="stocksvest_app")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the app API and sample UI")
    serve.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("APP_PORT", "8100")))
    args = parser.parse_args()

    import uvicorn

    from .api import create_app
    from .config import Settings

    uvicorn.run(create_app(Settings()), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
