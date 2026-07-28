"""Создаёт в amoCRM кастомные поля сделки, нужные конвейеру.

    python -m tools.setup_amo_fields

Идемпотентно: поле с таким же названием повторно не создаётся, скрипт просто
возьмёт существующий ID. Печатает готовые строки для .env.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voronka.amocrm import AmoClient  # noqa: E402
from voronka.config import Settings  # noqa: E402
from voronka.retry import AmoError  # noqa: E402
from voronka.store import Store  # noqa: E402

# название поля -> переменная окружения
FIELDS = [
    ("Источник заявки", "AMO_CF_SOURCE"),
    ("Бюджет", "AMO_CF_BUDGET"),
    ("Сроки", "AMO_CF_TIMELINE"),
    ("Telegram", "AMO_CF_TELEGRAM"),
]


async def main() -> None:
    s = Settings.load()
    store = Store(s.db_path)
    amo = AmoClient(s, store)

    existing: dict[str, int] = {}
    data = await amo._request("GET", "/api/v4/leads/custom_fields?limit=250")
    for f in (data or {}).get("_embedded", {}).get("custom_fields", []):
        existing[f["name"]] = f["id"]

    result: dict[str, int] = {}
    to_create = [(name, env) for name, env in FIELDS if name not in existing]

    for name, env in FIELDS:
        if name in existing:
            print(f"уже есть: {name} -> field_id={existing[name]}")
            result[env] = existing[name]

    if to_create:
        payload = [{"name": name, "type": "text", "is_api_only": False} for name, _ in to_create]
        try:
            created = await amo._request(
                "POST", "/api/v4/leads/custom_fields", json_body=payload
            )
        except AmoError as exc:
            print(f"не удалось создать поля: {exc}\nтело ответа: {exc.body}")
            await amo.aclose()
            store.close()
            return
        made = (created or {}).get("_embedded", {}).get("custom_fields", [])
        for field in made:
            env = next(e for n, e in FIELDS if n == field["name"])
            result[env] = field["id"]
            print(f"создано:  {field['name']} -> field_id={field['id']}")

    print("\nСтроки для .env:")
    for _, env in FIELDS:
        if env in result:
            print(f"{env}={result[env]}")

    await amo.aclose()
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
