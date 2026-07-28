"""Клиент amoCRM API v4.

Поддерживает оба режима авторизации:

  long_lived — долгосрочный токен (появился в феврале 2024). Выдаётся в карточке
               интеграции на срок от 1 дня до 5 лет, показывается ОДИН раз,
               refresh_token к нему не прилагается. Для приватной интеграции
               под один аккаунт это самый дешёвый путь.

  oauth      — authorization_code -> (access_token 24 ч, refresh_token 3 мес).
               refresh_token одноразовый: после обмена старый недействителен,
               поэтому новую пару обязательно сохранять, иначе доступ теряется
               и нужна повторная авторизация руками.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from .config import Settings
from .retry import PermanentError, RetryableError, classify_status
from .store import Store

# Запас, за который до истечения обновляем токен.
REFRESH_MARGIN_SECONDS = 300


class AmoClient:
    def __init__(self, settings: Settings, store: Store, client: httpx.AsyncClient | None = None):
        self.s = settings
        self.store = store
        self._client = client or httpx.AsyncClient(timeout=settings.amo_timeout_seconds)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------ токены

    async def _exchange(self, payload: dict) -> str:
        url = f"{self.s.amo_base_url}/oauth2/access_token"
        try:
            resp = await self._client.post(url, json=payload)
        except httpx.RequestError as exc:
            raise RetryableError(f"oauth transport: {exc!r}") from exc
        if resp.status_code >= 400:
            kind = classify_status(resp.status_code) or PermanentError
            raise kind(
                f"oauth {resp.status_code}", status=resp.status_code, body=resp.text[:500]
            )
        data = resp.json()
        expires_at = time.time() + float(data.get("expires_in", 86400))
        self.store.save_tokens(data["access_token"], data.get("refresh_token"), expires_at)
        return data["access_token"]

    async def _refresh(self, refresh_token: str) -> str:
        return await self._exchange(
            {
                "client_id": self.s.amo_client_id,
                "client_secret": self.s.amo_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "redirect_uri": self.s.amo_redirect_uri,
            }
        )

    async def access_token(self, force_refresh: bool = False) -> str:
        if self.s.amo_auth_mode == "long_lived":
            if not self.s.amo_long_lived_token:
                raise PermanentError("AMO_LONG_LIVED_TOKEN is empty")
            return self.s.amo_long_lived_token

        tokens = self.store.load_tokens()
        if tokens and not force_refresh and tokens["expires_at"] - REFRESH_MARGIN_SECONDS > time.time():
            return tokens["access_token"]
        if tokens and tokens.get("refresh_token"):
            return await self._refresh(tokens["refresh_token"])
        if self.s.amo_auth_code:
            return await self._exchange(
                {
                    "client_id": self.s.amo_client_id,
                    "client_secret": self.s.amo_client_secret,
                    "grant_type": "authorization_code",
                    "code": self.s.amo_auth_code,
                    "redirect_uri": self.s.amo_redirect_uri,
                }
            )
        raise PermanentError("no tokens and no AMO_AUTH_CODE to bootstrap OAuth")

    # -------------------------------------------------------------------- HTTP

    async def _request(self, method: str, path: str, *, json_body: Any = None, retry_auth: bool = True) -> Any:
        token = await self.access_token()
        url = f"{self.s.amo_base_url}{path}"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            resp = await self._client.request(method, url, json=json_body, headers=headers)
        except httpx.TimeoutException as exc:
            raise RetryableError(f"timeout: {exc!r}") from exc
        except httpx.RequestError as exc:
            raise RetryableError(f"transport: {exc!r}") from exc

        if resp.status_code == 401 and retry_auth and self.s.amo_auth_mode == "oauth":
            await self.access_token(force_refresh=True)
            return await self._request(method, path, json_body=json_body, retry_auth=False)

        kind = classify_status(resp.status_code)
        if kind is not None:
            raise kind(
                f"{method} {path} -> {resp.status_code}",
                status=resp.status_code,
                body=resp.text[:500],
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ------------------------------------------------------------------ методы

    def _cf(self, field_id: int | None, value: Any) -> dict | None:
        if not field_id or value in (None, ""):
            return None
        return {"field_id": field_id, "values": [{"value": value}]}

    async def create_lead_complex(
        self,
        *,
        name: str,
        contact_name: str,
        phone: str,
        email: str,
        telegram: str = "",
        price: int = 0,
        source: str = "",
        tags: list[str] | None = None,
    ) -> dict:
        """POST /api/v4/leads/complex — сделка + контакт одним запросом.

        Ответ: [{"id":..., "contact_id":..., "merged": bool}].
        merged=true означает, что сработал КОНТРОЛЬ ДУБЛЕЙ amoCRM и сделка
        привязалась к существующему контакту. Это его дедуп, не наш —
        см. README, раздел про измерение.
        """
        contact_cf = []
        if phone:
            contact_cf.append(
                {"field_code": "PHONE", "values": [{"enum_code": "WORK", "value": phone}]}
            )
        if email:
            contact_cf.append(
                {"field_code": "EMAIL", "values": [{"enum_code": "WORK", "value": email}]}
            )

        lead_cf = [
            cf
            for cf in (
                self._cf(self.s.cf_source, source),
                self._cf(self.s.cf_telegram, telegram),
            )
            if cf
        ]

        lead: dict[str, Any] = {
            "name": name,
            "price": price,
            "_embedded": {
                "contacts": [
                    {
                        "first_name": contact_name or "Без имени",
                        "custom_fields_values": contact_cf or None,
                    }
                ]
            },
        }
        if self.s.pipeline_id:
            lead["pipeline_id"] = self.s.pipeline_id
        if self.s.status_new:
            lead["status_id"] = self.s.status_new
        if lead_cf:
            lead["custom_fields_values"] = lead_cf
        if tags:
            lead["_embedded"]["tags"] = [{"name": t} for t in tags]

        data = await self._request("POST", "/api/v4/leads/complex", json_body=[lead])
        if not isinstance(data, list) or not data:
            raise PermanentError(f"unexpected complex response: {data!r}")
        return data[0]

    async def patch_lead(
        self,
        lead_id: int,
        *,
        status_id: int | None = None,
        budget: str = "",
        timeline: str = "",
        tags: list[str] | None = None,
    ) -> Any:
        body: dict[str, Any] = {}
        if status_id:
            body["status_id"] = status_id
        cf = [
            c
            for c in (
                self._cf(self.s.cf_budget, budget),
                self._cf(self.s.cf_timeline, timeline),
            )
            if c
        ]
        if cf:
            body["custom_fields_values"] = cf
        if tags:
            # ТОЛЬКО tags_to_add. `_embedded.tags` в PATCH ЗАМЕНЯЕТ весь список
            # тегов сделки: теги, поставленные при создании (voronka, form:...),
            # молча исчезают. Поймано на живом аккаунте — в ленте сделки видно
            # «Теги убраны: form:landing». См. docs/pitfalls.md, п. 14.
            body["tags_to_add"] = [{"name": t} for t in tags]
        if not body:
            return None
        return await self._request("PATCH", f"/api/v4/leads/{lead_id}", json_body=body)

    async def add_note(self, lead_id: int, text: str) -> Any:
        return await self._request(
            "POST",
            f"/api/v4/leads/{lead_id}/notes",
            json_body=[{"note_type": "common", "params": {"text": text}}],
        )

    async def get_lead(self, lead_id: int) -> Any:
        return await self._request("GET", f"/api/v4/leads/{lead_id}")

    async def count_leads(self) -> int:
        """Сколько сделок реально лежит в аккаунте (для замеров)."""
        total = 0
        page = 1
        while True:
            data = await self._request("GET", f"/api/v4/leads?page={page}&limit=250")
            if not data:
                break
            leads = data.get("_embedded", {}).get("leads", [])
            total += len(leads)
            if len(leads) < 250:
                break
            page += 1
        return total
