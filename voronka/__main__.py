"""Запуск: python -m voronka [--port 8080]"""
from __future__ import annotations

import argparse

import uvicorn

from .api import create_app
from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="voronka")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-worker", action="store_true", help="только HTTP, без доставки")
    args = parser.parse_args()

    settings = Settings.load()
    app = create_app(settings, run_worker=not args.no_worker)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
