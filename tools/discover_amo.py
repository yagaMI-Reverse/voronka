"""Достаёт из живого аккаунта amoCRM ID воронки, статусов и кастомных полей.

    python -m tools.discover_amo

Читает AMO_BASE_URL и токен из .env, печатает готовые строки для .env.
Без этого ID приходится выковыривать глазами из интерфейса — а они у каждого
аккаунта свои, и в этом половина боли первого подключения.
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


async def main() -> None:
    s = Settings.load()
    store = Store(s.db_path)
    amo = AmoClient(s, store)
    print(f"аккаунт: {s.amo_base_url}  режим авторизации: {s.amo_auth_mode}\n")

    try:
        pipelines = await amo._request("GET", "/api/v4/leads/pipelines")
    except AmoError as exc:
        print(f"не удалось получить воронки: {exc}\nтело ответа: {exc.body}")
        await amo.aclose()
        store.close()
        return

    print("=" * 72)
    print("ВОРОНКИ И СТАТУСЫ")
    print("=" * 72)
    suggestion: dict[str, int] = {}
    for p in (pipelines or {}).get("_embedded", {}).get("pipelines", []):
        mark = " (основная)" if p.get("is_main") else ""
        print(f"\nворонка «{p['name']}»{mark}  pipeline_id={p['id']}")
        if p.get("is_main") and "AMO_PIPELINE_ID" not in suggestion:
            suggestion["AMO_PIPELINE_ID"] = p["id"]
        for st in (p.get("_embedded") or {}).get("statuses", []):
            print(f"    status_id={st['id']:<12} {st['name']}  (сортировка {st.get('sort')})")
            if p.get("is_main") and st.get("type") == 1 and "AMO_STATUS_NEW" not in suggestion:
                suggestion["AMO_STATUS_NEW"] = st["id"]

    for entity in ("leads", "contacts"):
        try:
            fields = await amo._request("GET", f"/api/v4/{entity}/custom_fields?limit=250")
        except AmoError as exc:
            print(f"\nне удалось получить поля {entity}: {exc}")
            continue
        print("\n" + "=" * 72)
        print(f"КАСТОМНЫЕ ПОЛЯ: {entity}")
        print("=" * 72)
        items = (fields or {}).get("_embedded", {}).get("custom_fields", [])
        if not items:
            print("  (полей нет — создайте их в интерфейсе amoCRM)")
        for f in items:
            code = f.get("code") or "—"
            print(f"  field_id={f['id']:<12} {f['name']:<30} тип={f.get('type')}  код={code}")

    print("\n" + "=" * 72)
    print("СТРОКИ ДЛЯ .env (проверьте и допишите остальные ID руками)")
    print("=" * 72)
    for key, value in suggestion.items():
        print(f"{key}={value}")
    if not suggestion:
        print("# не удалось определить автоматически — возьмите ID из списков выше")

    await amo.aclose()
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
