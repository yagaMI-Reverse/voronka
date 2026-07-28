"""Интеграционные тесты воркера против мока amoCRM (через ASGI, без сети)."""
from __future__ import annotations

import asyncio

from voronka.models import BotHelpResult, FormLead
from voronka.pipeline import handle_bothelp, handle_form
from voronka.store import Store
from voronka.worker import Worker


def lead(**kw) -> FormLead:
    base = dict(name="Иван", phone="+7 707 123-45-67", email="ivan@example.com", comment="Хочу бота")
    base.update(kw)
    return FormLead(**base)


async def drain(worker: Worker, ticks: int = 30, pause: float = 0.02) -> None:
    for _ in range(ticks):
        await worker.tick()
        if worker.store.pending_count() == 0:
            return
        await asyncio.sleep(pause)


async def test_form_lead_reaches_crm(store: Store, worker: Worker, mock_reset):
    handle_form(store, lead())
    await drain(worker)

    assert len(mock_reset.state.leads) == 1
    created = next(iter(mock_reset.state.leads.values()))
    assert created["name"].startswith("Заявка с сайта")
    assert created["status_id"] == 142
    assert "voronka" in created["tags"]
    # Комментарий из формы стал примечанием
    assert any("Хочу бота" in n["text"] for n in mock_reset.state.notes)
    assert store.counts_by_kind()["delivered"] >= 1


async def test_duplicates_do_not_create_second_deal(store: Store, worker: Worker, mock_reset):
    for _ in range(5):
        handle_form(store, lead())
    await drain(worker)
    assert len(mock_reset.state.leads) == 1
    assert store.counts_by_kind()["duplicate"] == 4


async def test_outage_is_survived_and_event_is_not_lost(store: Store, worker: Worker, mock_reset):
    mock_reset.state.fault_mode = "http_503"
    mock_reset.state.fault_until = 9e18  # «внешний сервис лежит»
    handle_form(store, lead())

    await worker.tick()
    assert len(mock_reset.state.leads) == 0
    assert store.counts_by_kind()["retry"] == 1
    assert store.pending_count() == 1  # событие живо, лежит в очереди

    mock_reset.state.fault_mode = "off"
    mock_reset.state.fault_until = 0.0
    await drain(worker)

    assert len(mock_reset.state.leads) == 1
    assert store.counts_by_kind()["delivered"] == 1


async def test_exhausted_retries_land_in_dlq_and_can_be_replayed(
    store: Store, worker: Worker, mock_reset
):
    mock_reset.state.fault_mode = "http_503"
    mock_reset.state.fault_until = 9e18
    handle_form(store, lead())

    for _ in range(10):
        await worker.tick()
        await asyncio.sleep(0.02)

    assert store.outbox_by_state().get("dlq") == 1
    assert store.counts_by_kind()["dlq"] == 1
    assert len(mock_reset.state.leads) == 0

    # Ручной разбор: сервис починили, возвращаем задание в очередь
    mock_reset.state.fault_mode = "off"
    mock_reset.state.fault_until = 0.0
    assert len(store.requeue_dlq()) == 1
    await drain(worker)

    assert len(mock_reset.state.leads) == 1


async def test_permanent_error_goes_straight_to_dlq(store: Store, worker: Worker, mock_reset):
    """404 на несуществующей сделке — повторять бессмысленно."""
    ack = handle_bothelp(store, BotHelpResult(phone="87071234567", qualified=True))
    store.bind_amo_ids(ack.dedup_key, 999999, 888888)

    # create_lead уже не нужен: сделка «есть» в реестре
    for task in store.claim_due(limit=10):
        if task["op"] == "create_lead":
            store.mark_done(task["id"])
        else:
            await worker._process(task)

    assert store.outbox_by_state().get("dlq") == 1
    dlq = store.dlq()
    assert "permanent" in dlq[0]["last_error"]
    assert dlq[0]["attempts"] == 1, "перманентная ошибка не должна жечь попытки"


async def test_bot_result_updates_the_same_deal(store: Store, worker: Worker, mock_reset):
    handle_form(store, lead())
    await drain(worker)
    handle_bothelp(
        store,
        BotHelpResult(
            phone="8 707 123 45 67",
            qualified=True,
            budget="300-500 тыс ₸",
            timeline="в этом месяце",
            segment="hot",
            transcript="Бюджет: 300-500\nСроки: в этом месяце",
            step_id="qual",
        ),
    )
    await drain(worker)

    assert len(mock_reset.state.leads) == 1, "диалог бота не должен плодить вторую сделку"
    deal = next(iter(mock_reset.state.leads.values()))
    assert deal["status_id"] == 143, "статус переведён на «квалифицирован»"
    assert "hot" in deal["tags"]
    values = {
        cf["field_id"]: cf["values"][0]["value"] for cf in deal["custom_fields_values"]
    }
    assert values[1002] == "300-500 тыс ₸"
    assert values[1003] == "в этом месяце"
    assert any("Итог диалога в BotHelp" in n["text"] for n in mock_reset.state.notes)


async def test_bot_result_before_lead_creation_waits_instead_of_failing(
    store: Store, worker: Worker, mock_reset
):
    """Гонка: итог бота приехал раньше, чем создалась сделка."""
    handle_form(store, lead())
    handle_bothelp(store, BotHelpResult(phone="87071234567", qualified=True))

    tasks = store.claim_due(limit=10)
    update = [t for t in tasks if t["op"] == "update_lead"][0]
    create = [t for t in tasks if t["op"] == "create_lead"][0]

    await worker._process(update)  # сделки ещё нет
    assert store.counts_by_kind()["retry"] == 1
    assert store.outbox_by_state().get("dlq") is None

    await worker._process(create)
    await drain(worker)
    assert len(mock_reset.state.leads) == 1
    assert next(iter(mock_reset.state.leads.values()))["status_id"] == 143


async def test_expired_token_is_refreshed_transparently(
    store: Store, settings, mock_reset
):
    """OAuth-режим: 401 -> refresh -> повтор запроса, без потери события."""
    import dataclasses

    import httpx

    from mock_amo.app import app as mock_app
    from voronka.amocrm import AmoClient

    oauth_settings = dataclasses.replace(
        settings, amo_auth_mode="oauth", amo_auth_code="seed-code"
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_app), base_url="http://mock", timeout=5.0
    )
    amo = AmoClient(oauth_settings, store, client=client)
    worker = Worker(store, oauth_settings, amo)

    mock_reset.state.force_401 = 1
    handle_form(store, lead())
    await drain(worker)

    assert len(mock_reset.state.leads) == 1
    assert store.load_tokens()["refresh_token"].startswith("mock-refresh")
