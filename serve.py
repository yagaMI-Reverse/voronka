"""Демо-стенд одной командой: мок amoCRM на 8081 + Voronka на 8080.

    python serve.py

Лендинг:  http://127.0.0.1:8080/
Журнал:   http://127.0.0.1:8080/journal
Мок CRM:  http://127.0.0.1:8081/_control/leads

Для работы с БОЕВЫМ amoCRM мок не нужен: заполните .env и запускайте
`python -m voronka --port 8080`.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Дефолты демо-стенда: реальный .env, если он есть, имеет приоритет.
DEFAULTS = {
    "AMO_BASE_URL": "http://127.0.0.1:8081",
    "AMO_AUTH_MODE": "long_lived",
    "AMO_LONG_LIVED_TOKEN": "demo-long-lived-token",
    "AMO_PIPELINE_ID": "1300",
    "AMO_STATUS_NEW": "142",
    "AMO_STATUS_QUALIFIED": "143",
    "AMO_STATUS_REJECTED": "144",
    "AMO_CF_SOURCE": "1001",
    "AMO_CF_BUDGET": "1002",
    "AMO_CF_TIMELINE": "1003",
    "AMO_CF_TELEGRAM": "1004",
    "VORONKA_DB": str(ROOT / "demo.db"),
}
for key, value in DEFAULTS.items():
    os.environ.setdefault(key, value)

import uvicorn  # noqa: E402

from mock_amo.app import app as mock_app  # noqa: E402
from voronka.api import create_app  # noqa: E402
from voronka.config import Settings  # noqa: E402


def run_mock() -> None:
    uvicorn.run(mock_app, host="127.0.0.1", port=8081, log_level="warning")


def main() -> None:
    threading.Thread(target=run_mock, daemon=True).start()
    settings = Settings.load()
    print(f"[voronka] amoCRM: {settings.amo_base_url}  БД: {settings.db_path}")
    print("[voronka] лендинг http://127.0.0.1:8080/  журнал http://127.0.0.1:8080/journal")
    uvicorn.run(create_app(settings), host="127.0.0.1", port=8080, log_level="warning")


if __name__ == "__main__":
    main()
