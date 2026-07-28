"""Снимает скриншоты стенда в screenshots/ — воспроизводимо, одной командой.

    python -m bench.make_screenshots

Поднимает свой мок amoCRM и Voronka на отдельных портах, прогоняет демо-сценарий
(bench.seed_demo) и снимает страницы системным Chrome через Playwright.

Требуется playwright (см. requirements-dev.txt) и установленный Google Chrome —
браузеры Playwright скачивать не нужно, используется channel="chrome".
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "screenshots"
DB = ROOT / "bench" / "out" / "shots.db"
API = "http://127.0.0.1:8095"
MOCK = "http://127.0.0.1:8096"
VIEWPORT = {"width": 1440, "height": 950}


def wait_for(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2.0).status_code < 500:
                return
        except httpx.RequestError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} не поднялся")


def start() -> list[subprocess.Popen]:
    env = os.environ.copy()
    env.update(
        {
            "VORONKA_DB": str(DB),
            "AMO_BASE_URL": MOCK,
            "AMO_AUTH_MODE": "long_lived",
            "AMO_LONG_LIVED_TOKEN": "shots-token",
            "AMO_PIPELINE_ID": "1300",
            "AMO_STATUS_NEW": "142",
            "AMO_STATUS_QUALIFIED": "143",
            "AMO_STATUS_REJECTED": "144",
            "AMO_CF_SOURCE": "1001",
            "AMO_CF_BUDGET": "1002",
            "AMO_CF_TIMELINE": "1003",
            "AMO_CF_TELEGRAM": "1004",
            "RETRY_MAX_ATTEMPTS": "6",
            "RETRY_BASE_SECONDS": "1.0",
            "RETRY_MAX_SECONDS": "8.0",
            "WORKER_TICK_SECONDS": "0.1",
        }
    )
    py = sys.executable
    procs = [
        subprocess.Popen([py, "-m", "mock_amo", "--port", "8096"], cwd=ROOT, env=env),
        subprocess.Popen([py, "-m", "voronka", "--port", "8095"], cwd=ROOT, env=env),
    ]
    wait_for(f"{MOCK}/_control/state")
    wait_for(f"{API}/health")
    return procs


def stop(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


def shoot(page, path: Path, **kw) -> None:
    page.screenshot(path=str(path), **kw)
    print(f"  сохранено: {path.relative_to(ROOT)}")


def main() -> None:
    SHOTS.mkdir(exist_ok=True)
    DB.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB) + suffix)
        if p.exists():
            p.unlink()

    procs = start()
    try:
        from bench.seed_demo import run as seed

        print("наполняю стенд событиями…")
        seed(API, MOCK, verbose=False)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome")
            page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2, locale="ru-RU")

            print("снимаю лендинг…")
            page.goto(f"{API}/", wait_until="networkidle")
            page.wait_for_timeout(600)
            shoot(page, SHOTS / "01-landing.png")

            print("снимаю схему потока и блок надёжности…")
            page.evaluate("document.getElementById('flow').scrollIntoView()")
            page.wait_for_timeout(400)
            shoot(page, SHOTS / "02-flow-and-reliability.png")

            print("снимаю форму: заявка принята…")
            page.evaluate("document.getElementById('form').scrollIntoView()")
            page.fill("#name", "Марат Ахметов")
            page.fill("#phone", "+7 707 555 12 34")
            page.fill("#email", "marat@astana-shop.kz")
            page.fill("#telegram", "@marat_a")
            page.fill("#comment", "Нужен бот в Telegram и связка с amoCRM")
            page.click("#submitBtn")
            page.wait_for_selector("#result.show.ok", timeout=10_000)
            page.wait_for_timeout(400)
            shoot(page, SHOTS / "03-form-accepted.png")

            print("снимаю форму: повтор отсечён как дубль…")
            page.fill("#phone", "8 (707) 555-12-34")
            page.fill("#email", "MARAT@Astana-Shop.KZ")
            page.click("#submitBtn")
            page.wait_for_selector("#result.show.dup", timeout=10_000)
            page.wait_for_timeout(400)
            # снимаем карточку формы целиком: видно и телефон в другом формате,
            # и тот же ключ дедупликации в ответе
            page.locator(".form-card").screenshot(
                path=str(SHOTS / "04-form-duplicate-cut.png")
            )
            print(f"  сохранено: screenshots\\04-form-duplicate-cut.png")

            print("снимаю журнал событий…")
            page.goto(f"{API}/journal", wait_until="networkidle")
            page.wait_for_selector("#rows tr", timeout=10_000)
            page.wait_for_timeout(800)
            shoot(page, SHOTS / "05-journal.png")
            shoot(page, SHOTS / "06-journal-full.png", full_page=True)

            print("снимаю очередь ошибок…")
            page.evaluate("document.getElementById('dlqPanel').scrollIntoView({block:'center'})")
            page.wait_for_timeout(400)
            shoot(page, SHOTS / "07-dlq.png", clip=None)

            print("снимаю фильтр «дубль»…")
            page.click('button.chip[data-k="duplicate"]')
            page.wait_for_timeout(700)
            shoot(page, SHOTS / "08-journal-duplicates.png")

            browser.close()
    finally:
        stop(procs)

    print("\nГотово. Скриншоты в", SHOTS)


if __name__ == "__main__":
    main()
