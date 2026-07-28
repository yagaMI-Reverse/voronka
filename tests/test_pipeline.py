from voronka.models import BotHelpResult, FormLead
from voronka.pipeline import handle_bothelp, handle_form
from voronka.store import Store


def lead(**kw) -> FormLead:
    base = dict(name="Иван", phone="+7 707 123-45-67", email="ivan@example.com")
    base.update(kw)
    return FormLead(**base)


def test_first_submission_is_accepted(store: Store):
    ack = handle_form(store, lead())
    assert ack.status == "accepted"
    assert store.outbox_by_state() == {"pending": 1}


def test_redelivered_webhook_does_not_enqueue_twice(store: Store):
    handle_form(store, lead(request_id="r1"))
    ack = handle_form(store, lead(request_id="r2"))
    assert ack.status == "duplicate"
    assert store.outbox_by_state() == {"pending": 1}
    kinds = store.counts_by_kind()
    assert kinds["received"] == 2
    assert kinds["duplicate"] == 1


def test_same_person_different_phone_format_is_a_duplicate(store: Store):
    handle_form(store, lead(phone="+7 707 123-45-67"))
    ack = handle_form(store, lead(phone="8 (707) 123-45-67"))
    assert ack.status == "duplicate"


def test_lead_without_contact_is_rejected(store: Store):
    ack = handle_form(store, FormLead(name="Аноним"))
    assert ack.status == "rejected"
    assert store.outbox_by_state() == {}
    assert store.counts_by_kind()["rejected"] == 1


def test_bot_result_reuses_the_lead_created_by_the_form(store: Store):
    handle_form(store, lead())
    ack = handle_bothelp(
        store,
        BotHelpResult(phone="87071234567", qualified=True, budget="500k", step_id="qual"),
    )
    assert ack.status == "accepted"
    # create_lead + update_lead, но лид в реестре один
    assert store.outbox_by_state()["pending"] == 2
    assert store.inbox_size() == 2  # ключ лида + ключ шага бота


def test_repeated_bot_step_is_cut_off(store: Store):
    handle_form(store, lead())
    handle_bothelp(store, BotHelpResult(phone="87071234567", qualified=True, step_id="qual"))
    ack = handle_bothelp(store, BotHelpResult(phone="87071234567", qualified=True, step_id="qual"))
    assert ack.status == "duplicate"
    assert store.outbox_by_state()["pending"] == 2


def test_different_bot_steps_both_apply(store: Store):
    handle_form(store, lead())
    a = handle_bothelp(store, BotHelpResult(phone="87071234567", step_id="qual"))
    b = handle_bothelp(store, BotHelpResult(phone="87071234567", step_id="reminder-3"))
    assert a.status == "accepted" and b.status == "accepted"


def test_bot_only_contact_creates_the_lead(store: Store):
    """Человек пришёл сразу в бота, форму не заполнял."""
    ack = handle_bothelp(
        store, BotHelpResult(telegram_id="@ilya", qualified=True, step_id="qual")
    )
    assert ack.status == "accepted"
    ops = [t["op"] for t in store.claim_due(limit=10)]
    assert ops == ["create_lead", "update_lead"]
