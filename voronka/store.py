"""Хранилище: append-only журнал, реестр идемпотентности (inbox) и очередь (outbox).

Схема — три таблицы с разными свойствами:

  journal — ТОЛЬКО вставка. UPDATE/DELETE запрещены триггерами на уровне SQLite,
            а не договорённостью в коде. Это и есть «журнал событий»: что
            произошло, когда, с какой попытки, сколько заняло.

  inbox   — реестр натуральных ключей. PRIMARY KEY по dedup_key + вставка через
            INSERT OR IGNORE в одной транзакции = дубль отсекается атомарно,
            даже если два одинаковых вебхука пришли одновременно.

  outbox  — transactional outbox. HTTP-обработчик коммитит задание в БД и
            отвечает 202; доставкой занимается воркер. Поэтому падение amoCRM
            не теряет событие: оно лежит в outbox, пока не доставится или не
            уедет в DLQ.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS journal (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    trace_id    TEXT    NOT NULL,
    dedup_key   TEXT,
    source      TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    attempt     INTEGER NOT NULL DEFAULT 0,
    latency_ms  INTEGER,
    detail      TEXT,
    payload     TEXT
);

CREATE INDEX IF NOT EXISTS journal_dedup_idx ON journal(dedup_key);
CREATE INDEX IF NOT EXISTS journal_kind_idx  ON journal(kind);

-- Журнал append-only на уровне движка, а не на честном слове приложения.
CREATE TRIGGER IF NOT EXISTS journal_no_update
BEFORE UPDATE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;

CREATE TRIGGER IF NOT EXISTS journal_no_delete
BEFORE DELETE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;

CREATE TABLE IF NOT EXISTS inbox (
    dedup_key       TEXT PRIMARY KEY,
    first_trace_id  TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    hits            INTEGER NOT NULL DEFAULT 1,
    amo_lead_id     INTEGER,
    amo_contact_id  INTEGER
);

CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id        TEXT    NOT NULL,
    dedup_key       TEXT    NOT NULL,
    op              TEXT    NOT NULL,
    payload         TEXT    NOT NULL,
    state           TEXT    NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL    NOT NULL,
    received_ms     REAL    NOT NULL,
    last_error      TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS outbox_ready_idx ON outbox(state, next_attempt_at);

CREATE TABLE IF NOT EXISTS oauth_tokens (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    access_token  TEXT NOT NULL,
    refresh_token TEXT,
    expires_at    REAL NOT NULL,
    updated_at    TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ journal

    def log(
        self,
        *,
        trace_id: str,
        source: str,
        kind: str,
        dedup_key: str | None = None,
        attempt: int = 0,
        latency_ms: int | None = None,
        detail: str | None = None,
        payload: Any = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO journal (ts, trace_id, dedup_key, source, kind, attempt,"
                " latency_ms, detail, payload) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    utcnow(),
                    trace_id,
                    dedup_key,
                    source,
                    kind,
                    attempt,
                    latency_ms,
                    detail,
                    json.dumps(payload, ensure_ascii=False) if payload is not None else None,
                ),
            )
            self._conn.commit()

    def journal(self, limit: int = 200, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM journal"
        args: list[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            args.append(kind)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def counts_by_kind(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, COUNT(*) AS n FROM journal GROUP BY kind"
            ).fetchall()
        return {r["kind"]: r["n"] for r in rows}

    # -------------------------------------------------------------------- inbox

    def claim(self, dedup_key: str, trace_id: str) -> bool:
        """True — ключ увиден впервые (заявку надо обрабатывать).
        False — дубль, счётчик hits увеличен.

        Атомарно: INSERT OR IGNORE + rowcount в одной транзакции.
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO inbox (dedup_key, first_trace_id, created_at)"
                " VALUES (?,?,?)",
                (dedup_key, trace_id, utcnow()),
            )
            fresh = cur.rowcount == 1
            if not fresh:
                self._conn.execute(
                    "UPDATE inbox SET hits = hits + 1 WHERE dedup_key = ?", (dedup_key,)
                )
            self._conn.commit()
        return fresh

    def inbox_entry(self, dedup_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM inbox WHERE dedup_key = ?", (dedup_key,)
            ).fetchone()
        return dict(row) if row else None

    def bind_amo_ids(self, dedup_key: str, lead_id: int | None, contact_id: int | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE inbox SET amo_lead_id = ?, amo_contact_id = ? WHERE dedup_key = ?",
                (lead_id, contact_id, dedup_key),
            )
            self._conn.commit()

    def inbox_size(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]

    # ------------------------------------------------------------------- outbox

    def enqueue(
        self,
        *,
        trace_id: str,
        dedup_key: str,
        op: str,
        payload: dict,
        received_ms: float,
        delay: float = 0.0,
    ) -> int:
        now = time.monotonic()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO outbox (trace_id, dedup_key, op, payload, next_attempt_at,"
                " received_ms, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    trace_id,
                    dedup_key,
                    op,
                    json.dumps(payload, ensure_ascii=False),
                    now + delay,
                    received_ms,
                    utcnow(),
                    utcnow(),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def claim_due(self, limit: int = 10) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outbox WHERE state = 'pending' AND next_attempt_at <= ?"
                " ORDER BY id LIMIT ?",
                (now, limit),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                marks = ",".join("?" * len(ids))
                self._conn.execute(
                    f"UPDATE outbox SET state='inflight', updated_at=? WHERE id IN ({marks})",
                    [utcnow(), *ids],
                )
                self._conn.commit()
        return [dict(r) for r in rows]

    def mark_done(self, task_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE outbox SET state='done', attempts=attempts+1, updated_at=?, "
                "last_error=NULL WHERE id=?",
                (utcnow(), task_id),
            )
            self._conn.commit()

    def mark_retry(self, task_id: int, delay: float, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE outbox SET state='pending', attempts=attempts+1,"
                " next_attempt_at=?, last_error=?, updated_at=? WHERE id=?",
                (time.monotonic() + delay, error[:500], utcnow(), task_id),
            )
            self._conn.commit()

    def mark_dlq(self, task_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE outbox SET state='dlq', attempts=attempts+1, last_error=?,"
                " updated_at=? WHERE id=?",
                (error[:500], utcnow(), task_id),
            )
            self._conn.commit()

    def requeue_dlq(self, task_id: int | None = None) -> list[int]:
        """Ручной разбор очереди ошибок: вернуть задание(я) в работу."""
        with self._lock:
            if task_id is None:
                rows = self._conn.execute("SELECT id FROM outbox WHERE state='dlq'").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id FROM outbox WHERE state='dlq' AND id=?", (task_id,)
                ).fetchall()
            ids = [r["id"] for r in rows]
            for tid in ids:
                self._conn.execute(
                    "UPDATE outbox SET state='pending', attempts=0, next_attempt_at=?,"
                    " updated_at=? WHERE id=?",
                    (time.monotonic(), utcnow(), tid),
                )
            self._conn.commit()
        return ids

    def outbox_by_state(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM outbox GROUP BY state"
            ).fetchall()
        return {r["state"]: r["n"] for r in rows}

    def dlq(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outbox WHERE state='dlq' ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def pending_count(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE state IN ('pending','inflight')"
            ).fetchone()[0]

    # -------------------------------------------------------------- oauth tokens

    def save_tokens(self, access: str, refresh: str | None, expires_at: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO oauth_tokens (id, access_token, refresh_token, expires_at,"
                " updated_at) VALUES (1,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET access_token=excluded.access_token,"
                " refresh_token=excluded.refresh_token, expires_at=excluded.expires_at,"
                " updated_at=excluded.updated_at",
                (access, refresh, expires_at, utcnow()),
            )
            self._conn.commit()

    def load_tokens(self) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM oauth_tokens WHERE id=1").fetchone()
        return dict(row) if row else None

    # --------------------------------------------------------------------- misc

    def reset(self) -> None:
        """Только для прогонов/тестов: пересоздать файл БД."""
        with self._lock:
            self._conn.executescript(
                "DROP TRIGGER IF EXISTS journal_no_update;"
                "DROP TRIGGER IF EXISTS journal_no_delete;"
                "DROP TABLE IF EXISTS journal;"
                "DROP TABLE IF EXISTS inbox;"
                "DROP TABLE IF EXISTS outbox;"
                "DROP TABLE IF EXISTS oauth_tokens;"
            )
            self._conn.executescript(SCHEMA)
            self._conn.commit()


def rows_to_json(rows: Iterable[dict]) -> list[dict]:
    out = []
    for r in rows:
        r = dict(r)
        if r.get("payload"):
            try:
                r["payload"] = json.loads(r["payload"])
            except (TypeError, json.JSONDecodeError):
                pass
        out.append(r)
    return out
