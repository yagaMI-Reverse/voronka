"""Приём заявок: идемпотентность, журнал, постановка в очередь.

Ключевое решение: HTTP-обработчик НЕ ходит в amoCRM. Он пишет задание в outbox
и отвечает 202. Поэтому недоступность amoCRM не превращается в потерянную
заявку — событие уже в журнале и в очереди.
"""
from __future__ import annotations

import time
import uuid

from .keys import dedup_key
from .models import BotHelpResult, FormLead, WebhookAck
from .store import Store

# Область ключа для «личности лида» — одна и та же для формы и для бота,
# чтобы диалог в BotHelp приклеился к сделке, созданной с сайта.
LEAD_SCOPE = "lead"


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def lead_key(*, phone: str, email: str, telegram: str, form_id: str) -> str:
    return dedup_key(
        source=LEAD_SCOPE,
        phone=phone,
        email=email,
        telegram_id=telegram,
        form_id=form_id,
    )


def handle_form(store: Store, payload: FormLead, received_ms: float | None = None) -> WebhookAck:
    trace_id = new_trace_id()
    received_ms = received_ms if received_ms is not None else time.monotonic()

    try:
        key = lead_key(
            phone=payload.phone,
            email=payload.email,
            telegram=payload.telegram,
            form_id=payload.form_id,
        )
    except ValueError as exc:
        store.log(
            trace_id=trace_id,
            source="form",
            kind="rejected",
            detail=str(exc),
            payload=payload.model_dump(),
        )
        return WebhookAck(status="rejected", trace_id=trace_id, detail=str(exc))

    store.log(
        trace_id=trace_id,
        source="form",
        kind="received",
        dedup_key=key,
        detail=f"request_id={payload.request_id or '-'}",
        payload=payload.model_dump(),
    )

    if not store.claim(key, trace_id):
        entry = store.inbox_entry(key) or {}
        store.log(
            trace_id=trace_id,
            source="form",
            kind="duplicate",
            dedup_key=key,
            detail=(
                f"already seen at {entry.get('created_at')} "
                f"(trace={entry.get('first_trace_id')}, hits={entry.get('hits')})"
            ),
        )
        return WebhookAck(
            status="duplicate",
            trace_id=trace_id,
            dedup_key=key,
            detail="lead already registered",
        )

    store.enqueue(
        trace_id=trace_id,
        dedup_key=key,
        op="create_lead",
        payload=payload.model_dump(),
        received_ms=received_ms,
    )
    return WebhookAck(status="accepted", trace_id=trace_id, dedup_key=key)


def handle_bothelp(store: Store, payload: BotHelpResult, received_ms: float | None = None) -> WebhookAck:
    """Итог диалога бота -> обновление сделки.

    Два разных ключа:
      key_lead — личность (по ней ищем сделку, созданную формой);
      key_step — конкретный шаг сценария (по нему режем повторные доставки,
                 чтобы бот не насыпал в сделку десять одинаковых примечаний).
    """
    trace_id = new_trace_id()
    received_ms = received_ms if received_ms is not None else time.monotonic()

    try:
        key_l = lead_key(
            phone=payload.phone,
            email=payload.email,
            telegram=payload.telegram_id,
            form_id=payload.form_id,
        )
        key_step = dedup_key(
            source=f"bothelp:{payload.step_id}",
            phone=payload.phone,
            email=payload.email,
            telegram_id=payload.telegram_id,
            form_id=payload.form_id,
        )
    except ValueError as exc:
        store.log(
            trace_id=trace_id,
            source="bothelp",
            kind="rejected",
            detail=str(exc),
            payload=payload.model_dump(),
        )
        return WebhookAck(status="rejected", trace_id=trace_id, detail=str(exc))

    store.log(
        trace_id=trace_id,
        source="bothelp",
        kind="received",
        dedup_key=key_step,
        detail=f"step={payload.step_id} qualified={payload.qualified}",
        payload=payload.model_dump(),
    )

    if not store.claim(key_step, trace_id):
        store.log(
            trace_id=trace_id,
            source="bothelp",
            kind="duplicate",
            dedup_key=key_step,
            detail="bot step already applied",
        )
        return WebhookAck(status="duplicate", trace_id=trace_id, dedup_key=key_step)

    # Человек мог прийти сразу в бота, минуя форму: тогда сделки ещё нет.
    if store.claim(key_l, trace_id):
        store.log(
            trace_id=trace_id,
            source="bothelp",
            kind="received",
            dedup_key=key_l,
            detail="lead unknown, creating from bot dialog",
        )
        store.enqueue(
            trace_id=trace_id,
            dedup_key=key_l,
            op="create_lead",
            payload=FormLead(
                name=payload.telegram_id or "Из Telegram-бота",
                phone=payload.phone,
                email=payload.email,
                telegram=payload.telegram_id,
                form_id=payload.form_id,
                utm_source="bothelp",
            ).model_dump(),
            received_ms=received_ms,
        )

    store.enqueue(
        trace_id=trace_id,
        dedup_key=key_l,
        op="update_lead",
        payload=payload.model_dump(),
        received_ms=received_ms,
    )
    return WebhookAck(status="accepted", trace_id=trace_id, dedup_key=key_l)
