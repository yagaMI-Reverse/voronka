"""Стенд измерений. Поднимает мок amoCRM + Voronka и прогоняет 4 сценария.

    python -m bench.run_all

Пишет bench/out/results.json и bench/out/results.md. Все числа в README берутся
ТОЛЬКО отсюда: если сценарий не прогнан — в README стоит «не измерялось».

Стенд: локальный HTTP (127.0.0.1), мок amoCRM вместо боевого аккаунта.
Контроль дублей самой amoCRM в моке ВЫКЛЮЧЕН — иначе «ноль лишних сделок»
был бы заслугой CRM, а не слоя идемпотентности.
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
OUT = ROOT / "bench" / "out"
MOCK_URL = "http://127.0.0.1:8091"
API_URL = "http://127.0.0.1:8090"
DB = ROOT / "bench" / "out" / "bench.db"

TOTAL_SUBMISSIONS = 200
DUPLICATE_SUBMISSIONS = 60
OUTAGE_LEADS = 25
OUTAGE_SECONDS = 15
LATENCY_LEADS = 100
SINGLE_LEADS = 20
CONCURRENT_DUPES = 20


def log(msg: str) -> None:
    print(f"[bench] {msg}", flush=True)


def wait_for(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(url, timeout=2.0)
            if r.status_code < 500:
                return
        except httpx.RequestError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} не поднялся за {timeout}s")


def start_processes(retry_max_attempts: int = 8) -> list[subprocess.Popen]:
    env = os.environ.copy()
    env.update(
        {
            "VORONKA_DB": str(DB),
            "AMO_BASE_URL": MOCK_URL,
            "AMO_AUTH_MODE": "long_lived",
            "AMO_LONG_LIVED_TOKEN": "bench-long-lived-token",
            "AMO_PIPELINE_ID": "1300",
            "AMO_STATUS_NEW": "142",
            "AMO_STATUS_QUALIFIED": "143",
            "AMO_STATUS_REJECTED": "144",
            "AMO_CF_SOURCE": "1001",
            "AMO_CF_BUDGET": "1002",
            "AMO_CF_TIMELINE": "1003",
            "AMO_CF_TELEGRAM": "1004",
            "RETRY_MAX_ATTEMPTS": str(retry_max_attempts),
            "RETRY_BASE_SECONDS": "1.0",
            "RETRY_MAX_SECONDS": "8.0",
            "RETRY_JITTER": "1",
            "WORKER_TICK_SECONDS": "0.1",
            "VORONKA_WEBHOOK_TOKEN": "",
        }
    )
    py = sys.executable
    procs = [
        subprocess.Popen([py, "-m", "mock_amo", "--port", "8091"], cwd=ROOT, env=env),
        subprocess.Popen([py, "-m", "voronka", "--port", "8090"], cwd=ROOT, env=env),
    ]
    wait_for(f"{MOCK_URL}/_control/state")
    wait_for(f"{API_URL}/health")
    return procs


def stop(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


def reset_mock(duplicate_control: bool = False) -> None:
    httpx.post(
        f"{MOCK_URL}/_control/reset", json={"duplicate_control": duplicate_control}, timeout=10
    ).raise_for_status()


def mock_state() -> dict:
    return httpx.get(f"{MOCK_URL}/_control/state", timeout=10).json()


def api_stats() -> dict:
    return httpx.get(f"{API_URL}/api/stats", timeout=10).json()


def drain(timeout: float = 120.0) -> float:
    """Ждёт, пока очередь опустеет. Возвращает потраченное время."""
    t0 = time.perf_counter()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if api_stats()["pending"] == 0:
            return time.perf_counter() - t0
        time.sleep(0.2)
    raise RuntimeError("очередь не разобралась за отведённое время")


def person(i: int) -> dict:
    """Уникальная личность: 11 цифр, ни один индекс не совпадает с другим."""
    return {
        "name": f"Клиент {i}",
        "phone": f"+7 7{i:09d}",
        "email": f"client{i}@example.com",
        "comment": "Нужен чат-бот и интеграция с CRM",
        "form_id": "landing",
        "utm_source": "bench",
    }


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1)))))
    return ordered[k]


# --------------------------------------------------------------- сценарий A


def scenario_dedup(client: httpx.Client) -> dict:
    log(f"A. дедуп: {TOTAL_SUBMISSIONS} отправок, из них {DUPLICATE_SUBMISSIONS} дублей")
    reset_mock(duplicate_control=False)
    unique = TOTAL_SUBMISSIONS - DUPLICATE_SUBMISSIONS

    payloads = [person(i) for i in range(unique)]
    # Дубли: те же люди, но телефон записан в другом формате и другой request_id —
    # то есть на уровне байтов это ДРУГОЙ запрос.
    for i in range(DUPLICATE_SUBMISSIONS):
        src = dict(payloads[i % unique])
        digits = "".join(c for c in src["phone"] if c.isdigit())
        src["phone"] = "8" + digits[1:]          # +7... -> 8...
        src["email"] = src["email"].upper()      # регистр
        src["request_id"] = f"redelivery-{i}"
        payloads.append(src)

    accepted = duplicates = other = 0
    for p in payloads:
        r = client.post(f"{API_URL}/webhook/form", json=p, timeout=10)
        status = r.json().get("status")
        if status == "accepted":
            accepted += 1
        elif status == "duplicate":
            duplicates += 1
        else:
            other += 1

    drain_s = drain()
    state = mock_state()
    return {
        "submissions": len(payloads),
        "unique_people": unique,
        "intentional_duplicates": DUPLICATE_SUBMISSIONS,
        "accepted": accepted,
        "rejected_as_duplicate": duplicates,
        "other": other,
        "leads_in_crm": state["leads"],
        "extra_leads": state["leads"] - unique,
        "contacts_in_crm": state["contacts"],
        "amo_duplicate_control": state["duplicate_control"],
        "drain_seconds": round(drain_s, 2),
    }


# --------------------------------------------------------------- сценарий B


def scenario_outage(client: httpx.Client, *, offset: int, retry_budget: int) -> dict:
    log(
        f"B. отказ amoCRM на {OUTAGE_SECONDS}s при {OUTAGE_LEADS} заявках "
        f"(бюджет ретраев: {retry_budget})"
    )
    reset_mock(duplicate_control=False)
    stats_before = api_stats()["journal"]

    httpx.post(
        f"{MOCK_URL}/_control/fault",
        json={"mode": "http_503", "seconds": OUTAGE_SECONDS, "probability": 1.0},
        timeout=10,
    ).raise_for_status()

    t0 = time.perf_counter()
    for i in range(OUTAGE_LEADS):
        r = client.post(f"{API_URL}/webhook/form", json=person(offset + i), timeout=10)
        assert r.json()["status"] == "accepted", r.text
    accept_during_outage_s = time.perf_counter() - t0

    time.sleep(OUTAGE_SECONDS * 0.6)
    leads_during_outage = mock_state()["leads"]

    recovery_s = drain(timeout=180)
    after_retries = mock_state()["leads"]
    dlq_size = api_stats()["queue"].get("dlq", 0)

    # Второй рубеж: то, что исчерпало бюджет ретраев, лежит в DLQ и разбирается
    # руками. Проверяем, что replay действительно довозит событие.
    replayed = 0
    replay_s = 0.0
    if dlq_size:
        t_replay = time.perf_counter()
        replayed = len(
            httpx.post(f"{API_URL}/api/dlq/replay", json={}, timeout=20).json()["requeued"]
        )
        drain(timeout=120)
        replay_s = time.perf_counter() - t_replay

    state = mock_state()
    stats_after = api_stats()["journal"]
    retries = stats_after.get("retry", 0) - stats_before.get("retry", 0)

    return {
        "retry_budget": retry_budget,
        "leads_sent": OUTAGE_LEADS,
        "outage_seconds": OUTAGE_SECONDS,
        "http_503_returned_by_mock": state["fault_hits"],
        "accepted_while_crm_down_seconds": round(accept_during_outage_s, 2),
        "leads_in_crm_during_outage": leads_during_outage,
        "delivered_by_retry": after_retries,
        "landed_in_dlq": dlq_size,
        "recovered_by_dlq_replay": replayed,
        "replay_seconds": round(replay_s, 2),
        "leads_in_crm_final": state["leads"],
        "lost_events": OUTAGE_LEADS - state["leads"],
        "retry_events": retries,
        "dlq_left": api_stats()["queue"].get("dlq", 0),
        "time_to_recovery_seconds": round(recovery_s, 2),
    }


# --------------------------------------------------------------- сценарий C


def scenario_latency(client: httpx.Client) -> dict:
    log(f"C. латентность: {LATENCY_LEADS} заявок")
    reset_mock(duplicate_control=False)
    ack_ms: list[float] = []
    for i in range(LATENCY_LEADS):
        p = person(20_000 + i)
        t0 = time.perf_counter()
        r = client.post(f"{API_URL}/webhook/form", json=p, timeout=10)
        ack_ms.append((time.perf_counter() - t0) * 1000)
        assert r.json()["status"] == "accepted", r.text
    drain()

    events = httpx.get(
        f"{API_URL}/api/journal", params={"kind": "delivered", "limit": 1000}, timeout=20
    ).json()["events"]
    e2e = [e["latency_ms"] for e in events if e["latency_ms"] is not None][-LATENCY_LEADS:]

    # Второй режим: одиночная заявка на пустой очереди. Именно это видит живой
    # клиент, заполнивший форму, — без 99 соседей в очереди перед ним.
    single: list[float] = []
    for i in range(SINGLE_LEADS):
        r = client.post(f"{API_URL}/webhook/form", json=person(25_000 + i), timeout=10)
        assert r.json()["status"] == "accepted", r.text
        drain(timeout=30)
        last = httpx.get(
            f"{API_URL}/api/journal", params={"kind": "delivered", "limit": 1}, timeout=10
        ).json()["events"]
        if last and last[0]["latency_ms"] is not None:
            single.append(float(last[0]["latency_ms"]))

    return {
        "leads": LATENCY_LEADS,
        "single_leads": len(single),
        "single_form_to_crm_ms": {
            "p50": round(pct(single, 50), 1),
            "p95": round(pct(single, 95), 1),
            "max": round(max(single), 1) if single else None,
            "mean": round(statistics.fmean(single), 1) if single else None,
        },
        "ack_ms": {
            "p50": round(pct(ack_ms, 50), 1),
            "p95": round(pct(ack_ms, 95), 1),
            "max": round(max(ack_ms), 1),
            "mean": round(statistics.fmean(ack_ms), 1),
        },
        "form_to_crm_ms": {
            "p50": round(pct(e2e, 50), 1),
            "p95": round(pct(e2e, 95), 1),
            "max": round(max(e2e), 1) if e2e else None,
            "mean": round(statistics.fmean(e2e), 1) if e2e else None,
            "samples": len(e2e),
        },
    }


# --------------------------------------------------------------- сценарий D


def scenario_concurrent(client: httpx.Client) -> dict:
    log(f"D. одновременные дубли: {CONCURRENT_DUPES} параллельных одинаковых отправок")
    reset_mock(duplicate_control=False)
    from concurrent.futures import ThreadPoolExecutor

    payload = person(30_001)
    variants = []
    for i in range(CONCURRENT_DUPES):
        v = dict(payload)
        v["request_id"] = f"parallel-{i}"
        variants.append(v)

    def send(p: dict) -> str:
        with httpx.Client(timeout=15) as c:
            return c.post(f"{API_URL}/webhook/form", json=p).json()["status"]

    with ThreadPoolExecutor(max_workers=CONCURRENT_DUPES) as pool:
        statuses = list(pool.map(send, variants))
    drain()
    state = mock_state()
    return {
        "parallel_submissions": CONCURRENT_DUPES,
        "accepted": statuses.count("accepted"),
        "duplicate": statuses.count("duplicate"),
        "leads_in_crm": state["leads"],
    }


# ------------------------------------------------------------------ рендер


def outage_table(b: dict) -> list[str]:
    return [
        "| метрика | значение |",
        "| --- | --- |",
        f"| бюджет ретраев | {b['retry_budget']} попыток |",
        f"| заявок отправлено | {b['leads_sent']} |",
        f"| CRM отвечает 503 | {b['outage_seconds']} с |",
        f"| ответов 503 отдано моком | {b['http_503_returned_by_mock']} |",
        f"| приём заявок во время аварии | все {b['leads_sent']} приняты за "
        f"{b['accepted_while_crm_down_seconds']} с |",
        f"| сделок в CRM во время аварии | {b['leads_in_crm_during_outage']} |",
        f"| событий ушло в ретрай | {b['retry_events']} |",
        f"| доехало ретраями | {b['delivered_by_retry']} |",
        f"| исчерпало бюджет и легло в DLQ | {b['landed_in_dlq']} |",
        f"| довезено ручным replay из DLQ | {b['recovered_by_dlq_replay']} "
        f"(за {b['replay_seconds']} с) |",
        f"| **сделок в CRM в итоге** | **{b['leads_in_crm_final']}** |",
        f"| **потеряно событий** | **{b['lost_events']}** |",
        f"| осталось в DLQ | {b['dlq_left']} |",
        f"| очередь разобрана за | {b['time_to_recovery_seconds']} с |",
    ]


def to_markdown(results: dict) -> str:
    a, b, c, d = results["dedup"], results["outage"], results["latency"], results["concurrent"]
    b2 = results["outage_wide_budget"]
    env = results["env"]
    lines = [
        "# Измерения",
        "",
        f"Прогон: `{results['ts']}`  ",
        f"Стенд: {env['platform']}, Python {env['python']}, всё на 127.0.0.1  ",
        "amoCRM: **мок** (`mock_amo`), контроль дублей самой CRM выключен  ",
        f"Ретраи: max {env['retry_max_attempts']} попыток, base {env['retry_base_seconds']}s, "
        f"cap {env['retry_max_seconds']}s, полный джиттер",
        "",
        "Воспроизвести: `python -m bench.run_all`",
        "",
        "## A. Идемпотентность",
        "",
        "| метрика | значение |",
        "| --- | --- |",
        f"| отправлено заявок | {a['submissions']} |",
        f"| из них уникальных людей | {a['unique_people']} |",
        f"| намеренных дублей | {a['intentional_duplicates']} |",
        f"| принято к обработке | {a['accepted']} |",
        f"| отсечено как дубль | {a['rejected_as_duplicate']} |",
        f"| **сделок в CRM** | **{a['leads_in_crm']}** |",
        f"| **лишних сделок** | **{a['extra_leads']}** |",
        f"| контактов в CRM | {a['contacts_in_crm']} |",
        f"| очередь разобрана за | {a['drain_seconds']} с |",
        "",
        "Дубли отправлялись НЕ байт-в-байт: телефон в другом формате (`+7…` → `8…`),",
        "email в другом регистре, свой `request_id`. Дедуп работает по нормализованному",
        "натуральному ключу, а не по идентификатору доставки.",
        "",
        "## B. Отказ внешнего сервиса",
        "",
        f"amoCRM отдаёт 503 в течение {b['outage_seconds']} секунд, пока в неё едут "
        f"{b['leads_sent']} заявок.",
        "",
        f"### B1. Бюджет ретраев {b['retry_budget']} попыток (значение по умолчанию)",
        "",
        *outage_table(b),
        "",
        f"**Что вскрылось:** бюджета в {b['retry_budget']} попыток при cap "
        f"{env['retry_max_seconds']}s не хватило на аварию в {b['outage_seconds']} с — "
        f"{b['landed_in_dlq']} события из {b['leads_sent']} сожгли все попытки и легли в DLQ.",
        "Для этого DLQ и существует: ручной replay довёз их, итог — "
        f"{b['leads_in_crm_final']} из {b['leads_sent']}, потерь {b['lost_events']}.",
        "",
        f"### B2. Бюджет ретраев {b2['retry_budget']} попыток",
        "",
        *outage_table(b2),
        "",
        f"**Вывод:** бюджет ретраев подбирается под ожидаемую длительность аварии, "
        f"а не «на глаз». При {b2['retry_budget']} попытках ту же аварию пережили "
        f"{b2['delivered_by_retry']} из {b2['leads_sent']} без ручного вмешательства "
        f"(в DLQ ушло {b2['landed_in_dlq']}).",
        "",
        "## C. Время прохождения заявки",
        "",
        "| метрика | p50 | p95 | max | среднее |",
        "| --- | --- | --- | --- | --- |",
        f"| ответ формы (202 Accepted) | {c['ack_ms']['p50']} мс | {c['ack_ms']['p95']} мс | "
        f"{c['ack_ms']['max']} мс | {c['ack_ms']['mean']} мс |",
        f"| форма → карточка в CRM, **одиночная заявка** | "
        f"{c['single_form_to_crm_ms']['p50']} мс | {c['single_form_to_crm_ms']['p95']} мс | "
        f"{c['single_form_to_crm_ms']['max']} мс | {c['single_form_to_crm_ms']['mean']} мс |",
        f"| форма → карточка в CRM, **залп {c['leads']} заявок разом** | "
        f"{c['form_to_crm_ms']['p50']} мс | {c['form_to_crm_ms']['p95']} мс | "
        f"{c['form_to_crm_ms']['max']} мс | {c['form_to_crm_ms']['mean']} мс |",
        "",
        f"Выборка: {c['single_leads']} одиночных заявок и залп из {c['leads']} "
        f"({c['form_to_crm_ms']['samples']} доставок).",
        "",
        "Две строки — не одно и то же, и путать их нельзя:",
        "",
        "* **одиночная заявка** — то, что видит живой клиент: 202 сразу, карточка в CRM",
        "  через один тик воркера + один запрос к API;",
        "* **залп** — 100 заявок встают в очередь и разбираются одним воркером",
        "  последовательно, поэтому p50 растёт: это плата за один воркер, а не за архитектуру.",
        "",
        "Обе цифры сняты **против мока на localhost**: они показывают накладные расходы",
        "самого конвейера. На боевом amoCRM сверху добавится сеть и время их API —",
        "против живого аккаунта не измерялось.",
        "",
        "## D. Одновременные дубли (гонка)",
        "",
        "| метрика | значение |",
        "| --- | --- |",
        f"| параллельных одинаковых отправок | {d['parallel_submissions']} |",
        f"| принято | {d['accepted']} |",
        f"| отсечено как дубль | {d['duplicate']} |",
        f"| **сделок в CRM** | **{d['leads_in_crm']}** |",
        "",
        "Дедуп через `INSERT OR IGNORE` по первичному ключу — победитель ровно один",
        "даже когда все запросы приходят в одну миллисекунду.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB) + suffix)
        if p.exists():
            p.unlink()

    results = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "env": {
            "platform": f"{sys.platform}",
            "python": sys.version.split()[0],
            "retry_max_attempts": 8,
            "retry_base_seconds": 1.0,
            "retry_max_seconds": 8.0,
        },
    }

    procs = start_processes(retry_max_attempts=8)
    try:
        with httpx.Client() as client:
            results["dedup"] = scenario_dedup(client)
            results["outage"] = scenario_outage(client, offset=100_000, retry_budget=8)
            results["latency"] = scenario_latency(client)
            results["concurrent"] = scenario_concurrent(client)
    finally:
        stop(procs)

    # Тот же сценарий аварии с увеличенным бюджетом ретраев — требует рестарта
    # процесса, потому что бюджет читается из окружения при старте.
    procs = start_processes(retry_max_attempts=14)
    try:
        with httpx.Client() as client:
            results["outage_wide_budget"] = scenario_outage(
                client, offset=200_000, retry_budget=14
            )
    finally:
        stop(procs)

    (OUT / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = to_markdown(results)
    (OUT / "results.md").write_text(md, encoding="utf-8")
    (ROOT / "docs" / "measurements.md").parent.mkdir(exist_ok=True)
    (ROOT / "docs" / "measurements.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
