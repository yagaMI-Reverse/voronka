"""Мок amoCRM API v4 с управляемыми сбоями.

Зачем: чтобы цифры в README были получены на воспроизводимом стенде, а не
«на глазок в триале». Мок повторяет те части контракта, которые использует
Voronka: OAuth2, /api/v4/leads/complex, PATCH сделки, примечания, список сделок.

Отдельно управляем двумя вещами:
  * контроль дублей самой amoCRM — по умолчанию ВЫКЛЮЧЕН, чтобы «ноль лишних
    сделок» был заслугой нашего слоя идемпотентности, а не CRM;
  * сбои — /_control/fault включает 503 или таймауты на заданное время.
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import Body, FastAPI, Header, Request
from fastapi.responses import JSONResponse


@dataclass
class State:
    leads: dict[int, dict] = field(default_factory=dict)
    contacts: dict[int, dict] = field(default_factory=dict)
    notes: list[dict] = field(default_factory=list)
    next_id: int = 10000
    # Контроль дублей amoCRM: при True сделка привязывается к существующему
    # контакту с тем же телефоном/email и возвращается merged=true.
    duplicate_control: bool = False
    # off | http_400 | http_429 | http_500 | http_503 | timeout
    fault_mode: str = "off"
    fault_until: float = 0.0
    fault_probability: float = 1.0
    fault_hits: int = 0
    force_401: int = 0
    requests: int = 0

    def new_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def fault_active(self) -> bool:
        return self.fault_mode != "off" and time.time() < self.fault_until


FAULT_STATUS = {
    "http_400": 400,
    "http_429": 429,
    "http_500": 500,
    "http_503": 503,
}

state = State()
app = FastAPI(title="mock-amocrm", version="1.0.0")


def _digits(s: str) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


def _contact_identity(contact: dict) -> tuple[str, str]:
    phone = email = ""
    for cf in contact.get("custom_fields_values") or []:
        code = (cf.get("field_code") or "").upper()
        values = cf.get("values") or []
        if not values:
            continue
        value = str(values[0].get("value", ""))
        if code == "PHONE":
            phone = _digits(value)
        elif code == "EMAIL":
            email = value.strip().lower()
    return phone, email


@app.middleware("http")
async def fault_injection(request: Request, call_next):
    if request.url.path.startswith("/_control"):
        return await call_next(request)
    state.requests += 1
    if state.fault_active() and random.random() < state.fault_probability:
        state.fault_hits += 1
        if state.fault_mode == "timeout":
            await asyncio.sleep(60)
            return JSONResponse({"detail": "should have timed out"}, status_code=200)
        code = FAULT_STATUS.get(state.fault_mode, 503)
        detail = (
            "Field 'custom_fields_values' is invalid"  # так выглядит типичная 400 от amoCRM
            if code == 400
            else "mock outage"
        )
        return JSONResponse({"detail": detail}, status_code=code)
    return await call_next(request)


def _auth_error(authorization: str | None) -> JSONResponse | None:
    if not authorization or not authorization.startswith("Bearer ") or len(authorization) < 10:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    if state.force_401 > 0:
        state.force_401 -= 1
        return JSONResponse({"detail": "token expired"}, status_code=401)
    return None


# --------------------------------------------------------------------- OAuth2


@app.post("/oauth2/access_token")
async def access_token(body: dict = Body(...)):
    grant = body.get("grant_type")
    if grant == "authorization_code":
        if not body.get("code"):
            return JSONResponse({"detail": "invalid code"}, status_code=400)
    elif grant == "refresh_token":
        if not body.get("refresh_token"):
            return JSONResponse({"detail": "invalid refresh_token"}, status_code=400)
    else:
        return JSONResponse({"detail": "unsupported grant_type"}, status_code=400)
    stamp = int(time.time())
    return {
        "token_type": "Bearer",
        "expires_in": 86400,
        "access_token": f"mock-access-{stamp}",
        "refresh_token": f"mock-refresh-{stamp}",
    }


# ---------------------------------------------------------------------- leads


@app.post("/api/v4/leads/complex")
async def leads_complex(body: list[dict] = Body(...), authorization: str | None = Header(default=None)):
    if (err := _auth_error(authorization)) is not None:
        return err
    out = []
    for lead in body:
        embedded = lead.get("_embedded") or {}
        contacts = embedded.get("contacts") or [{}]
        contact = contacts[0]
        phone, email = _contact_identity(contact)

        contact_id = None
        merged = False
        if state.duplicate_control and (phone or email):
            for cid, existing in state.contacts.items():
                if (phone and existing.get("phone") == phone) or (
                    email and existing.get("email") == email
                ):
                    contact_id = cid
                    merged = True
                    break
        if contact_id is None:
            contact_id = state.new_id()
            state.contacts[contact_id] = {
                "id": contact_id,
                "name": contact.get("first_name") or "",
                "phone": phone,
                "email": email,
            }

        lead_id = state.new_id()
        state.leads[lead_id] = {
            "id": lead_id,
            "name": lead.get("name"),
            "price": lead.get("price", 0),
            "status_id": lead.get("status_id"),
            "pipeline_id": lead.get("pipeline_id"),
            "custom_fields_values": lead.get("custom_fields_values") or [],
            "contact_id": contact_id,
            "tags": [t.get("name") for t in (embedded.get("tags") or []) if t.get("name")],
            "created_at": int(time.time()),
        }
        out.append({"id": lead_id, "contact_id": contact_id, "merged": merged})
    return out


@app.patch("/api/v4/leads/{lead_id}")
async def patch_lead(lead_id: int, body: dict = Body(...), authorization: str | None = Header(default=None)):
    if (err := _auth_error(authorization)) is not None:
        return err
    lead = state.leads.get(lead_id)
    if lead is None:
        return JSONResponse({"detail": "lead not found"}, status_code=404)
    if "status_id" in body and body["status_id"] is not None:
        lead["status_id"] = body["status_id"]
    for cf in body.get("custom_fields_values") or []:
        existing = [c for c in lead["custom_fields_values"] if c.get("field_id") != cf.get("field_id")]
        existing.append(cf)
        lead["custom_fields_values"] = existing
    for tag in (body.get("_embedded") or {}).get("tags") or []:
        if tag.get("name") and tag["name"] not in lead["tags"]:
            lead["tags"].append(tag["name"])
    lead["updated_at"] = int(time.time())
    return {"id": lead_id, "updated_at": lead["updated_at"]}


@app.post("/api/v4/leads/{lead_id}/notes")
async def add_notes(lead_id: int, body: list[dict] = Body(...), authorization: str | None = Header(default=None)):
    if (err := _auth_error(authorization)) is not None:
        return err
    if lead_id not in state.leads:
        return JSONResponse({"detail": "lead not found"}, status_code=404)
    created = []
    for note in body:
        note_id = state.new_id()
        state.notes.append(
            {
                "id": note_id,
                "entity_id": lead_id,
                "note_type": note.get("note_type", "common"),
                "text": (note.get("params") or {}).get("text", ""),
                "created_at": int(time.time()),
            }
        )
        created.append({"id": note_id})
    return {"_embedded": {"notes": created}}


@app.get("/api/v4/leads/{lead_id}")
async def get_lead(lead_id: int, authorization: str | None = Header(default=None)):
    if (err := _auth_error(authorization)) is not None:
        return err
    lead = state.leads.get(lead_id)
    if lead is None:
        return JSONResponse({"detail": "lead not found"}, status_code=404)
    notes = [n for n in state.notes if n["entity_id"] == lead_id]
    return {**lead, "_embedded": {"notes": notes}}


@app.get("/api/v4/leads")
async def list_leads(page: int = 1, limit: int = 250, authorization: str | None = Header(default=None)):
    if (err := _auth_error(authorization)) is not None:
        return err
    items = sorted(state.leads.values(), key=lambda x: x["id"])
    chunk = items[(page - 1) * limit : page * limit]
    if not chunk:
        return JSONResponse(status_code=204, content=None)
    return {"_page": page, "_embedded": {"leads": chunk}}


# -------------------------------------------------------------------- control


@app.post("/_control/reset")
async def control_reset(body: dict = Body(default={})):
    global state
    state = State(duplicate_control=bool(body.get("duplicate_control", False)))
    return {"ok": True, "duplicate_control": state.duplicate_control}


@app.post("/_control/fault")
async def control_fault(body: dict = Body(...)):
    """{"mode":"http_503","seconds":10,"probability":1.0} | {"mode":"off"}"""
    state.fault_mode = body.get("mode", "off")
    state.fault_probability = float(body.get("probability", 1.0))
    seconds = float(body.get("seconds", 0))
    state.fault_until = time.time() + seconds if state.fault_mode != "off" else 0.0
    return {
        "mode": state.fault_mode,
        "until": state.fault_until,
        "probability": state.fault_probability,
    }


@app.post("/_control/expire_token")
async def control_expire(body: dict = Body(default={})):
    state.force_401 = int(body.get("times", 1))
    return {"force_401": state.force_401}


@app.get("/_control/state")
async def control_state() -> dict[str, Any]:
    return {
        "leads": len(state.leads),
        "contacts": len(state.contacts),
        "notes": len(state.notes),
        "requests": state.requests,
        "fault_mode": state.fault_mode,
        "fault_hits": state.fault_hits,
        "fault_active": state.fault_active(),
        "duplicate_control": state.duplicate_control,
    }


@app.get("/_control/leads")
async def control_leads():
    return {
        "leads": sorted(state.leads.values(), key=lambda x: x["id"]),
        "contacts": sorted(state.contacts.values(), key=lambda x: x["id"]),
        "notes": state.notes,
    }
