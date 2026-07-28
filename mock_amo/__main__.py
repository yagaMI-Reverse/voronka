"""Запуск: python -m mock_amo [--port 8081]"""
from __future__ import annotations

import argparse

import uvicorn

from .app import app


def main() -> None:
    parser = argparse.ArgumentParser(prog="mock_amo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
