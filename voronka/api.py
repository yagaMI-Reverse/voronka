"""HTTP-слой: приём вебхуков, журнал, очередь ошибок."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .amocrm import AmoClient
from .config import ROOT, Settings
from .models import BotHelpResult, FormLead
from .pipeline import handle_bothelp, handle_form, lead_key
from .store import Store, rows_to_json
from .worker import Worker

WEB_DIR = ROOT / "web"


def create_app(settings: Settings | None = None, run_worker: bool = True) -> FastAPI:
    s = settings or Settings.load()
    store = Store(s.db_path)
    amo = AmoClient(s, store)
    worker = Worker(store, s, amo)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(worker.run()) if run_worker else None
        try:
            yield
        finally:
            if task:
                worker.stop()
                await asyncio.wait_for(task, timeout=5)
            await amo.aclose()
            store.close()

    app = FastAPI(title="Voronka", version="1.0.0", lifespan=lifespan)
    app.state.store = store
    app.state.settings = s
    app.state.worker = worker
    app.state.amo = amo

    def check_token(token: str | None) -> None:
        if s.webhook_token and token != s.webhook_token:
            raise HTTPException(status_code=401, detail="bad X-Voronka-Token")

    # ----------------------------------------------------------------- вебхуки

    @app.post("/webhook/form")
    async def webhook_form(
        payload: FormLead,
        x_voronka_token: str | None = Header(default=None),
    ):
        check_token(x_voronka_token)
        ack = handle_form(store, payload, received_ms=time.monotonic())
        code = {"accepted": 202, "duplicate": 200, "rejected": 422}[ack.status]
        return JSONResponse(status_code=code, content=ack.model_dump())

    @app.post("/webhook/bothelp")
    async def webhook_bothelp(
        payload: BotHelpResult,
        x_voronka_token: str | None = Header(default=None),
    ):
        """Принимает действие «Внешний запрос» из BotHelp.

        Ответ намеренно плоский: BotHelp вытаскивает значения JSON Path'ом вида
        $.status и $.lead_id и кладёт их в поля подписчика. Content-Type
        application/json обязателен с обеих сторон, иначе маппинг не сработает.
        """
        check_token(x_voronka_token)
        ack = handle_bothelp(store, payload, received_ms=time.monotonic())
        # ID сделки ищем по ключу ЛИЧНОСТИ, а не по ключу шага: повторный шаг
        # тоже должен получить в ответе номер сделки для маппинга в BotHelp.
        entry = None
        if ack.status != "rejected":
            try:
                entry = store.inbox_entry(
                    lead_key(
                        phone=payload.phone,
                        email=payload.email,
                        telegram=payload.telegram_id,
                        form_id=payload.form_id,
                    )
                )
            except ValueError:
                entry = None
        code = {"accepted": 202, "duplicate": 200, "rejected": 422}[ack.status]
        return JSONResponse(
            status_code=code,
            content={
                "status": ack.status,
                "trace_id": ack.trace_id,
                "lead_id": (entry or {}).get("amo_lead_id"),
                "detail": ack.detail or "",
            },
        )

    # ---------------------------------------------------------------- наблюдение

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "amo_base_url": s.amo_base_url,
            "auth_mode": s.amo_auth_mode,
            "queue": store.outbox_by_state(),
        }

    @app.get("/api/journal")
    async def journal(limit: int = 200, kind: str | None = None):
        return {"events": rows_to_json(store.journal(limit=limit, kind=kind))}

    @app.get("/api/stats")
    async def stats():
        return {
            "journal": store.counts_by_kind(),
            "queue": store.outbox_by_state(),
            "unique_leads": store.inbox_size(),
            "pending": store.pending_count(),
        }

    @app.get("/api/dlq")
    async def dlq():
        return {"tasks": rows_to_json(store.dlq())}

    @app.post("/api/dlq/replay")
    async def dlq_replay(body: dict = Body(default={})):
        task_id = body.get("id")
        ids = store.requeue_dlq(int(task_id) if task_id else None)
        for tid in ids:
            store.log(
                trace_id="operator",
                source="dlq",
                kind="requeued",
                detail=f"outbox task {tid} returned to queue manually",
            )
        return {"requeued": ids}

    # ------------------------------------------------------------------ статика

    @app.get("/")
    async def index():
        path = WEB_DIR / "index.html"
        if not path.exists():
            return JSONResponse({"detail": "web/index.html not built"}, status_code=404)
        return FileResponse(path)

    @app.get("/journal")
    async def journal_page():
        path = WEB_DIR / "journal.html"
        if not path.exists():
            return JSONResponse({"detail": "web/journal.html not built"}, status_code=404)
        return FileResponse(path)

    return app


app_factory = create_app
