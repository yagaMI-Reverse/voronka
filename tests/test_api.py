"""Тесты HTTP-слоя: коды ответов, защита токеном, форма ответа для BotHelp."""
from __future__ import annotations

import dataclasses

import httpx
import pytest

from voronka.api import create_app


@pytest.fixture
def client(settings):
    app = create_app(settings, run_worker=False)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), app


async def test_form_status_codes(client):
    c, _ = client
    lead = {"name": "Иван", "phone": "+7 707 123 45 67"}

    first = await c.post("/webhook/form", json=lead)
    assert first.status_code == 202
    assert first.json()["status"] == "accepted"

    again = await c.post("/webhook/form", json={"name": "Иван", "phone": "87071234567"})
    assert again.status_code == 200
    assert again.json()["status"] == "duplicate"

    empty = await c.post("/webhook/form", json={"name": "Аноним"})
    assert empty.status_code == 422
    assert empty.json()["status"] == "rejected"


async def test_bothelp_response_is_flat_for_json_path(client):
    """BotHelp достаёт значения JSON Path'ом — ответ должен быть плоским."""
    c, app = client
    await c.post("/webhook/form", json={"name": "Иван", "phone": "+77071234567"})

    # эмулируем доставку: сделка «создана»
    store = app.state.store
    key = store.journal(limit=50)[-1]["dedup_key"]
    store.bind_amo_ids(key, 15198335, 19663157)

    res = await c.post(
        "/webhook/bothelp",
        json={"phone": "8 707 123 45 67", "qualified": True, "step_id": "qualification"},
    )
    assert res.status_code == 202
    body = res.json()
    assert set(body) == {"status", "trace_id", "lead_id", "detail"}
    assert body["lead_id"] == 15198335, "ID сделки должен вернуться для маппинга в поле подписчика"

    # повтор того же шага: тоже должен отдавать lead_id, иначе BotHelp
    # затрёт поле подписчика пустым значением
    repeat = await c.post(
        "/webhook/bothelp",
        json={"phone": "+77071234567", "qualified": True, "step_id": "qualification"},
    )
    assert repeat.status_code == 200
    assert repeat.json()["status"] == "duplicate"
    assert repeat.json()["lead_id"] == 15198335


async def test_webhook_token_is_enforced(settings):
    guarded = dataclasses.replace(settings, webhook_token="s3cret")
    app = create_app(guarded, run_worker=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        lead = {"phone": "+77071234567"}

        denied = await c.post("/webhook/form", json=lead)
        assert denied.status_code == 401

        wrong = await c.post("/webhook/form", json=lead, headers={"X-Voronka-Token": "nope"})
        assert wrong.status_code == 401

        allowed = await c.post("/webhook/form", json=lead, headers={"X-Voronka-Token": "s3cret"})
        assert allowed.status_code == 202


async def test_stats_and_dlq_endpoints(client):
    c, app = client
    await c.post("/webhook/form", json={"phone": "+77071234567"})

    stats = (await c.get("/api/stats")).json()
    assert stats["unique_leads"] == 1
    assert stats["journal"]["received"] == 1
    assert stats["queue"] == {"pending": 1}

    store = app.state.store
    task_id = store.claim_due(limit=1)[0]["id"]
    store.mark_dlq(task_id, "boom")

    dlq = (await c.get("/api/dlq")).json()
    assert len(dlq["tasks"]) == 1

    replay = (await c.post("/api/dlq/replay", json={})).json()
    assert replay["requeued"] == [task_id]
    assert (await c.get("/api/dlq")).json()["tasks"] == []
