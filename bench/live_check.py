"""Сквозной прогон против ЖИВОГО аккаунта amoCRM.

    python -m bench.live_check

Берёт настройки из .env (боевой поддомен + долгосрочный токен), поднимает
конвейер на 8090 и проверяет четыре вещи на реальном API:

  1. заявка с формы доезжает до карточки сделки — с замером времени;
  2. повтор с другим форматом телефона не создаёт второй сделки;
  3. итог диалога бота обновляет ТУ ЖЕ сделку: статус, поля, примечание;
  4. латентность форма -> карточка на N одиночных заявках.

Мок здесь не участвует. Инъекции сбоев тоже: ронять чужой прод нельзя,
сценарий аварии остаётся на стенде с моком (bench/run_all.py).

Каждый прогон создаёт в аккаунте LATENCY_LEADS + 1 сделку. Всё, что можно
измерить на моке, ДОЛЖНО измеряться на моке: не засоряйте боевую воронку
служебными лидами.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voronka.config import Settings  # noqa: E402

API = "http://127.0.0.1:8090"
LATENCY_LEADS = 10
STAMP = time.strftime("%d.%m %H:%M")


def wait_for(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2.0).status_code < 500:
                return
        except httpx.RequestError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} не поднялся")


def start() -> subprocess.Popen:
    env = os.environ.copy()
    env["VORONKA_DB"] = str(ROOT / "live.db")
    proc = subprocess.Popen([sys.executable, "-m", "voronka", "--port", "8090"], cwd=ROOT, env=env)
    wait_for(f"{API}/health")
    return proc


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


class Amo:
    def __init__(self, s: Settings):
        self.base = s.amo_base_url
        self.headers = {
            "Authorization": f"Bearer {s.amo_long_lived_token}",
            "Content-Type": "application/json",
        }

    def get(self, path: str):
        r = httpx.get(f"{self.base}{path}", headers=self.headers, timeout=20)
        if r.status_code == 204:
            return None
        r.raise_for_status()
        return r.json()

    def count_leads(self) -> int:
        total, page = 0, 1
        while True:
            data = self.get(f"/api/v4/leads?page={page}&limit=250")
            if not data:
                break
            chunk = data.get("_embedded", {}).get("leads", [])
            total += len(chunk)
            if len(chunk) < 250:
                break
            page += 1
        return total

    def lead(self, lead_id: int):
        return self.get(f"/api/v4/leads/{lead_id}?with=contacts")

    def notes(self, lead_id: int):
        data = self.get(f"/api/v4/leads/{lead_id}/notes")
        return (data or {}).get("_embedded", {}).get("notes", [])


# ВАЖНО: один клиент на весь прогон. httpx.post() без клиента поднимает новое
# соединение на каждый запрос, и в замер ответа формы попадает установка
# соединения, а не работа конвейера. На этом уже один раз обожглись: получили
# «202 за 170 мс» там, где на постоянном соединении 1.2 мс.
CLIENT = httpx.Client(timeout=20)


def post(path: str, payload: dict) -> dict:
    return CLIENT.post(f"{API}{path}", json=payload).json()


def wait_quiet(timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if httpx.get(f"{API}/api/stats", timeout=10).json()["pending"] == 0:
            return
        time.sleep(0.3)
    raise RuntimeError("очередь не разобралась")


def last_delivered_latency() -> float | None:
    events = httpx.get(
        f"{API}/api/journal", params={"kind": "delivered", "limit": 1}, timeout=10
    ).json()["events"]
    if events and events[0]["latency_ms"] is not None:
        return float(events[0]["latency_ms"])
    return None


def main() -> None:
    s = Settings.load()
    if "amocrm" not in s.amo_base_url:
        raise SystemExit(f"AMO_BASE_URL={s.amo_base_url} — это не боевой аккаунт, прогон отменён")
    amo = Amo(s)
    print(f"аккаунт: {s.amo_base_url}")

    proc = start()
    report: dict = {"account": s.amo_base_url, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        leads_before = amo.count_leads()
        print(f"сделок в аккаунте до прогона: {leads_before}")

        # --- 1. заявка с формы -------------------------------------------------
        phone = "+7 707 640 22 18"
        lead_payload = {
            "name": f"Айгерим Сатпаева ({STAMP})",
            "phone": phone,
            "email": "aigerim@kaspi-shop.kz",
            "telegram": "@aigerim_s",
            "comment": "Нужна автоворонка в Telegram и выгрузка заявок в amoCRM",
            "form_id": "landing",
            "utm_source": "instagram",
        }
        t0 = time.perf_counter()
        ack = post("/webhook/form", lead_payload)
        ack_ms = (time.perf_counter() - t0) * 1000
        print(f"1. форма -> {ack['status']}, ответ за {ack_ms:.1f} мс")
        assert ack["status"] == "accepted", ack
        wait_quiet()
        e2e_ms = last_delivered_latency()

        entry = httpx.get(f"{API}/api/journal", params={"limit": 200}, timeout=10).json()["events"]
        dedup_key = ack["dedup_key"]
        stats = httpx.get(f"{API}/api/stats", timeout=10).json()
        assert stats["queue"].get("dlq", 0) == 0, f"что-то улетело в DLQ: {stats}"

        leads_after_first = amo.count_leads()
        created = leads_after_first - leads_before
        print(f"   сделок создано: {created}, форма -> карточка: {e2e_ms:.0f} мс")
        assert created == 1, f"ожидалась одна сделка, создано {created}"

        # находим ID сделки в реестре конвейера
        dlq = httpx.get(f"{API}/api/dlq", timeout=10).json()
        lead_id = None
        for ev in entry:
            if ev["kind"] == "delivered" and ev["dedup_key"] == dedup_key:
                lead_id = None
                break
        state = httpx.get(f"{API}/api/stats", timeout=10).json()
        # ID берём из самой CRM: последняя созданная сделка
        all_leads = amo.get("/api/v4/leads?limit=250&order[created_at]=desc")
        lead_id = all_leads["_embedded"]["leads"][0]["id"]
        card = amo.lead(lead_id)
        print(f"   сделка id={lead_id}: «{card['name']}», статус {card['status_id']}")

        # --- 2. повтор с другим форматом телефона ------------------------------
        dup_payload = dict(lead_payload)
        dup_payload["phone"] = "8 (707) 640-22-18"
        dup_payload["email"] = "AIGERIM@Kaspi-Shop.KZ"
        dup_payload["request_id"] = "retry-from-site"
        dup = post("/webhook/form", dup_payload)
        wait_quiet()
        leads_after_dup = amo.count_leads()
        print(f"2. повтор -> {dup['status']}, сделок в CRM: {leads_after_dup}")
        assert dup["status"] == "duplicate", dup
        assert leads_after_dup == leads_after_first, "повтор создал вторую сделку!"

        # --- 3. итог диалога бота ---------------------------------------------
        bot = post(
            "/webhook/bothelp",
            {
                "phone": "+77076402218",
                "telegram_id": "@aigerim_s",
                "qualified": True,
                "budget": "300–500 тыс ₸",
                "timeline": "до конца месяца",
                "segment": "hot",
                "transcript": (
                    "Ниша: доставка еды\nБюджет: 300–500 тыс ₸\n"
                    "Сроки: до конца месяца\nЕсть ли CRM: да, amoCRM"
                ),
                "step_id": "qualification",
            },
        )
        wait_quiet()
        print(f"3. итог бота -> {bot['status']}, lead_id из ответа: {bot['lead_id']}")
        card = amo.lead(lead_id)
        notes = amo.notes(lead_id)
        values = {
            cf["field_id"]: cf["values"][0]["value"]
            for cf in (card.get("custom_fields_values") or [])
        }
        leads_after_bot = amo.count_leads()
        print(f"   статус сделки: {card['status_id']} (ожидался {s.status_qualified})")
        print(f"   поля: бюджет={values.get(s.cf_budget)!r} сроки={values.get(s.cf_timeline)!r}")
        print(f"   примечаний: {len(notes)}, сделок в CRM: {leads_after_bot}")
        assert leads_after_bot == leads_after_first, "диалог бота создал лишнюю сделку!"
        assert card["status_id"] == s.status_qualified, "статус не переведён"
        tags = [t["name"] for t in (card.get("_embedded") or {}).get("tags") or []]
        print(f"   теги: {tags}")
        assert {"voronka", "form:landing", "bothelp", "hot"} <= set(tags), (
            f"теги от создания сделки затёрты обновлением: {tags}"
        )
        assert any("BotHelp" in (n.get("params") or {}).get("text", "") for n in notes), (
            "примечание с итогом диалога не найдено"
        )

        # --- 4. латентность на одиночных заявках -------------------------------
        print(f"4. латентность на {LATENCY_LEADS} одиночных заявках…")
        acks: list[float] = []
        e2e: list[float] = []
        for i in range(LATENCY_LEADS):
            p = {
                "name": f"Тест конвейера {i + 1} ({STAMP})",
                "phone": f"+7 705 {i:03d} 77 {i:02d}",
                "email": f"pipeline{i}@voronka.test",
                "comment": "Замер времени прохождения заявки",
                "form_id": "latency",
                "utm_source": "bench",
            }
            t = time.perf_counter()
            r = post("/webhook/form", p)
            acks.append((time.perf_counter() - t) * 1000)
            assert r["status"] == "accepted", r
            wait_quiet()
            lat = last_delivered_latency()
            if lat is not None:
                e2e.append(lat)

        stats = httpx.get(f"{API}/api/stats", timeout=10).json()
        leads_final = amo.count_leads()

        report.update(
            {
                "leads_before": leads_before,
                "leads_final": leads_final,
                "first_lead_id": lead_id,
                "ack_ms_first": round(ack_ms, 1),
                "e2e_ms_first": round(e2e_ms, 1) if e2e_ms else None,
                "duplicate_blocked": True,
                "bot_updated_same_lead": True,
                "latency_samples": len(e2e),
                "ack_ms": {
                    "p50": round(statistics.median(acks), 1),
                    "max": round(max(acks), 1),
                },
                "form_to_crm_ms": {
                    "p50": round(statistics.median(e2e), 1),
                    "min": round(min(e2e), 1),
                    "max": round(max(e2e), 1),
                    "mean": round(statistics.fmean(e2e), 1),
                },
                "journal": stats["journal"],
                "queue": stats["queue"],
            }
        )
    finally:
        stop(proc)

    out = ROOT / "bench" / "out" / "live_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 64)
    print("ИТОГ ПРОТИВ ЖИВОГО amoCRM")
    print("=" * 64)
    print(f"сделок в аккаунте: было {report['leads_before']}, стало {report['leads_final']}")
    print(f"ответ формы (202): p50 {report['ack_ms']['p50']} мс, max {report['ack_ms']['max']} мс")
    f = report["form_to_crm_ms"]
    print(
        f"форма -> карточка в CRM: p50 {f['p50']} мс, min {f['min']} мс, "
        f"max {f['max']} мс, среднее {f['mean']} мс ({report['latency_samples']} заявок)"
    )
    print(f"журнал: {report['journal']}")
    print(f"очередь: {report['queue']}")
    print(f"\nотчёт: {out}")


if __name__ == "__main__":
    main()
