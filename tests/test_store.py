import sqlite3

import pytest

from voronka.store import Store


def test_journal_rejects_update(store: Store):
    store.log(trace_id="t1", source="form", kind="received", dedup_key="k1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("UPDATE journal SET kind='tampered' WHERE seq=1")


def test_journal_rejects_delete(store: Store):
    store.log(trace_id="t1", source="form", kind="received", dedup_key="k1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("DELETE FROM journal WHERE seq=1")


def test_claim_is_first_writer_wins(store: Store):
    assert store.claim("key-a", "trace-1") is True
    assert store.claim("key-a", "trace-2") is False
    assert store.claim("key-a", "trace-3") is False
    entry = store.inbox_entry("key-a")
    assert entry["first_trace_id"] == "trace-1"
    assert entry["hits"] == 3
    assert store.inbox_size() == 1


def test_claim_survives_parallel_threads(store: Store):
    """Одновременные одинаковые вебхуки: победитель ровно один."""
    import threading

    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(16)

    def worker(i: int):
        barrier.wait()
        got = store.claim("race-key", f"trace-{i}")
        with lock:
            results.append(got)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 1, f"ожидался один победитель, получено {sum(results)}"
    assert store.inbox_entry("race-key")["hits"] == 16


def test_dlq_replay_returns_task_to_queue(store: Store):
    task_id = store.enqueue(
        trace_id="t", dedup_key="k", op="create_lead", payload={}, received_ms=0.0
    )
    store.mark_dlq(task_id, "boom")
    assert store.outbox_by_state() == {"dlq": 1}
    assert store.requeue_dlq() == [task_id]
    assert store.outbox_by_state() == {"pending": 1}
