from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mock_amo.app import app as mock_app, state as mock_state  # noqa: E402
from voronka.amocrm import AmoClient  # noqa: E402
from voronka.config import Settings  # noqa: E402
from voronka.store import Store  # noqa: E402
from voronka.worker import Worker  # noqa: E402


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = Settings(
        db_path=tmp_path / "test.db",
        amo_base_url="http://mock",
        amo_auth_mode="long_lived",
        amo_long_lived_token="test-long-lived-token",
        amo_client_id="cid",
        amo_client_secret="secret",
        amo_redirect_uri="https://example.com/cb",
        amo_auth_code="",
        amo_timeout_seconds=5.0,
        pipeline_id=1300,
        status_new=142,
        status_qualified=143,
        status_rejected=144,
        cf_source=1001,
        cf_budget=1002,
        cf_timeline=1003,
        cf_telegram=1004,
        retry_max_attempts=4,
        retry_base_seconds=0.01,
        retry_max_seconds=0.05,
        retry_jitter=False,
        worker_tick_seconds=0.01,
        webhook_token="",
    )
    return dataclasses.replace(base, **overrides)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def store(settings: Settings) -> Store:
    s = Store(settings.db_path)
    yield s
    s.close()


@pytest.fixture
def mock_reset():
    from mock_amo import app as mock_module

    mock_module.state = mock_module.State()
    return mock_module


@pytest.fixture
def amo(settings: Settings, store: Store, mock_reset):
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock", timeout=5.0
    )
    return AmoClient(settings, store, client=client)


@pytest.fixture
def worker(store: Store, settings: Settings, amo: AmoClient) -> Worker:
    return Worker(store, settings, amo)
