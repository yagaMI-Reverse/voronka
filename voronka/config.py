"""Конфигурация из переменных окружения (.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    load_dotenv(ROOT / ".env")


def _int(name: str, default: int | None = None) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    db_path: Path
    amo_base_url: str
    amo_auth_mode: str
    amo_long_lived_token: str
    amo_client_id: str
    amo_client_secret: str
    amo_redirect_uri: str
    amo_auth_code: str
    amo_timeout_seconds: float
    pipeline_id: int | None
    status_new: int | None
    status_qualified: int | None
    status_rejected: int | None
    cf_source: int | None
    cf_budget: int | None
    cf_timeline: int | None
    cf_telegram: int | None
    retry_max_attempts: int
    retry_base_seconds: float
    retry_max_seconds: float
    retry_jitter: bool
    worker_tick_seconds: float
    webhook_token: str

    @classmethod
    def load(cls) -> "Settings":
        _load_env()
        db = os.getenv("VORONKA_DB", "voronka.db")
        db_path = Path(db)
        if not db_path.is_absolute():
            db_path = ROOT / db_path
        return cls(
            db_path=db_path,
            amo_base_url=os.getenv("AMO_BASE_URL", "http://127.0.0.1:8081").rstrip("/"),
            amo_auth_mode=os.getenv("AMO_AUTH_MODE", "long_lived").strip(),
            amo_long_lived_token=os.getenv("AMO_LONG_LIVED_TOKEN", "").strip(),
            amo_client_id=os.getenv("AMO_CLIENT_ID", "").strip(),
            amo_client_secret=os.getenv("AMO_CLIENT_SECRET", "").strip(),
            amo_redirect_uri=os.getenv("AMO_REDIRECT_URI", "").strip(),
            amo_auth_code=os.getenv("AMO_AUTH_CODE", "").strip(),
            amo_timeout_seconds=_float("AMO_TIMEOUT_SECONDS", 10.0),
            pipeline_id=_int("AMO_PIPELINE_ID"),
            status_new=_int("AMO_STATUS_NEW"),
            status_qualified=_int("AMO_STATUS_QUALIFIED"),
            status_rejected=_int("AMO_STATUS_REJECTED"),
            cf_source=_int("AMO_CF_SOURCE"),
            cf_budget=_int("AMO_CF_BUDGET"),
            cf_timeline=_int("AMO_CF_TIMELINE"),
            cf_telegram=_int("AMO_CF_TELEGRAM"),
            retry_max_attempts=_int("RETRY_MAX_ATTEMPTS", 6) or 6,
            retry_base_seconds=_float("RETRY_BASE_SECONDS", 1.0),
            retry_max_seconds=_float("RETRY_MAX_SECONDS", 60.0),
            retry_jitter=_bool("RETRY_JITTER", True),
            worker_tick_seconds=_float("WORKER_TICK_SECONDS", 0.25),
            webhook_token=os.getenv("VORONKA_WEBHOOK_TOKEN", "").strip(),
        )
