"""Воркер очереди: доставка в amoCRM, ретраи с backoff, DLQ.

Один воркер, задания берутся по возрастанию id — порядок внутри одного лида
сохраняется (сначала create_lead, потом update_lead). Горизонтальное
масштабирование потребовало бы шардирования по dedup_key; здесь не сделано
и в README это заявлено честно.
"""
from __future__ import annotations

import asyncio
import json
import time

from .amocrm import AmoClient
from .config import Settings
from .models import BotHelpResult, FormLead
from .retry import PermanentError, RetryableError, backoff_delay
from .store import Store


class Worker:
    def __init__(self, store: Store, settings: Settings, client: AmoClient):
        self.store = store
        self.s = settings
        self.amo = client
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.tick()
            except Exception as exc:  # noqa: BLE001 — воркер не должен умирать
                self.store.log(
                    trace_id="worker",
                    source="worker",
                    kind="error",
                    detail=f"tick failed: {exc!r}",
                )
                processed = 0
            if processed == 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.s.worker_tick_seconds)
                except asyncio.TimeoutError:
                    pass

    async def tick(self, limit: int = 20) -> int:
        tasks = self.store.claim_due(limit=limit)
        for task in tasks:
            await self._process(task)
        return len(tasks)

    async def _process(self, task: dict) -> None:
        attempt = int(task["attempts"]) + 1
        payload = json.loads(task["payload"])
        source = "form" if task["op"] == "create_lead" else "bothelp"
        try:
            if task["op"] == "create_lead":
                await self._create_lead(task, payload)
            elif task["op"] == "update_lead":
                await self._update_lead(task, payload)
            else:
                raise PermanentError(f"unknown op {task['op']!r}")
        except PermanentError as exc:
            self.store.mark_dlq(task["id"], f"permanent: {exc} body={exc.body}")
            self.store.log(
                trace_id=task["trace_id"],
                source=source,
                kind="dlq",
                dedup_key=task["dedup_key"],
                attempt=attempt,
                detail=f"permanent {exc.status}: {exc}",
            )
        except RetryableError as exc:
            self._reschedule(task, attempt, source, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._reschedule(task, attempt, source, f"unexpected: {exc!r}")
        else:
            latency_ms = int((time.monotonic() - float(task["received_ms"])) * 1000)
            self.store.mark_done(task["id"])
            self.store.log(
                trace_id=task["trace_id"],
                source=source,
                kind="delivered",
                dedup_key=task["dedup_key"],
                attempt=attempt,
                latency_ms=latency_ms,
                detail=task["op"],
            )

    def _reschedule(self, task: dict, attempt: int, source: str, error: str) -> None:
        if attempt >= self.s.retry_max_attempts:
            self.store.mark_dlq(task["id"], error)
            self.store.log(
                trace_id=task["trace_id"],
                source=source,
                kind="dlq",
                dedup_key=task["dedup_key"],
                attempt=attempt,
                detail=f"gave up after {attempt} attempts: {error}",
            )
            return
        delay = backoff_delay(
            attempt,
            base=self.s.retry_base_seconds,
            cap=self.s.retry_max_seconds,
            jitter=self.s.retry_jitter,
        )
        self.store.mark_retry(task["id"], delay, error)
        self.store.log(
            trace_id=task["trace_id"],
            source=source,
            kind="retry",
            dedup_key=task["dedup_key"],
            attempt=attempt,
            detail=f"retry in {delay:.2f}s: {error}",
        )

    # ------------------------------------------------------------------ операции

    async def _create_lead(self, task: dict, payload: dict) -> None:
        lead = FormLead(**payload)
        entry = self.store.inbox_entry(task["dedup_key"]) or {}
        if entry.get("amo_lead_id"):
            # Сделка уже создана более ранней попыткой, которая упала после
            # успешного ответа amoCRM. Повтор не должен создавать вторую.
            return
        title = f"Заявка с сайта — {lead.name or lead.phone or lead.email}"
        result = await self.amo.create_lead_complex(
            name=title,
            contact_name=lead.name,
            phone=lead.phone,
            email=lead.email,
            telegram=lead.telegram,
            source=lead.utm_source or lead.form_id,
            tags=["voronka", f"form:{lead.form_id}"],
        )
        self.store.bind_amo_ids(
            task["dedup_key"], result.get("id"), result.get("contact_id")
        )
        if lead.comment:
            await self.amo.add_note(int(result["id"]), f"Комментарий из формы:\n{lead.comment}")

    async def _update_lead(self, task: dict, payload: dict) -> None:
        res = BotHelpResult(**payload)
        entry = self.store.inbox_entry(task["dedup_key"]) or {}
        lead_id = entry.get("amo_lead_id")
        if not lead_id:
            # Сделка ещё создаётся (create_lead в очереди или в ретрае).
            # Это ретраебельная ситуация, а не ошибка.
            raise RetryableError("lead is not created yet, waiting for create_lead")

        status = self.s.status_qualified if res.qualified else self.s.status_rejected
        tags = ["bothelp"]
        if res.segment:
            tags.append(res.segment)
        await self.amo.patch_lead(
            int(lead_id),
            status_id=status,
            budget=res.budget,
            timeline=res.timeline,
            tags=tags,
        )
        note = [
            f"Итог диалога в BotHelp (шаг {res.step_id}):",
            f"квалифицирован: {'да' if res.qualified else 'нет'}",
        ]
        if res.budget:
            note.append(f"бюджет: {res.budget}")
        if res.timeline:
            note.append(f"сроки: {res.timeline}")
        if res.segment:
            note.append(f"сегмент: {res.segment}")
        if res.transcript:
            note.append(f"\nответы:\n{res.transcript}")
        await self.amo.add_note(int(lead_id), "\n".join(note))
