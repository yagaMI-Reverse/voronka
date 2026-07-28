"""Поднимает конвейер и публичный тоннель к нему — для подключения BotHelp.

    python -m tools.expose

Что делает:
  1. проверяет, что VORONKA_WEBHOOK_TOKEN не пуст (без него эндпоинт наружу
     выставлять нельзя: писать в CRM сможет любой, кто узнает адрес);
  2. запускает конвейер на 8080 с конфигом из .env;
  3. поднимает ngrok и забирает публичный https-адрес;
  4. проверяет, что снаружи действительно отвечает наш сервис и что запрос
     без правильного токена получает 401;
  5. печатает готовый блок настроек для действия «Внешний запрос» в BotHelp.

Адрес ngrok живёт до остановки процесса. Перезапустили — адрес другой,
и его надо переписать в BotHelp. Это ограничение тоннеля, не конвейера.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voronka.config import Settings  # noqa: E402

PORT = 8080
NGROK_API = "http://127.0.0.1:4040/api/tunnels"


def wait_http(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2.0).status_code < 500:
                return
        except httpx.RequestError:
            time.sleep(0.3)
    raise RuntimeError(f"{url} не поднялся за {timeout} с")


def public_url(timeout: float = 40.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = httpx.get(NGROK_API, timeout=3.0).json()
            for t in data.get("tunnels", []):
                if t.get("public_url", "").startswith("https://"):
                    return t["public_url"]
        except (httpx.RequestError, json.JSONDecodeError):
            pass
        time.sleep(0.5)
    raise RuntimeError("ngrok не отдал публичный адрес")


def main() -> None:
    s = Settings.load()
    if not s.webhook_token:
        raise SystemExit(
            "VORONKA_WEBHOOK_TOKEN пуст. Выставлять эндпоинт наружу без общего\n"
            "секрета нельзя: заявки в вашу CRM сможет слать кто угодно.\n"
            "Сгенерируйте значение и впишите в .env."
        )
    if "amocrm" in s.amo_base_url:
        print(f"внимание: конвейер настроен на ЖИВОЙ аккаунт {s.amo_base_url}")

    env = os.environ.copy()
    procs: list[subprocess.Popen] = []
    try:
        procs.append(
            subprocess.Popen(
                [sys.executable, "-m", "voronka", "--port", str(PORT)], cwd=ROOT, env=env
            )
        )
        wait_http(f"http://127.0.0.1:{PORT}/health")
        print(f"конвейер поднят на http://127.0.0.1:{PORT}")

        procs.append(
            subprocess.Popen(
                ["ngrok", "http", str(PORT), "--log", "stdout"],
                cwd=ROOT,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=True,
            )
        )
        url = public_url()
        print(f"публичный адрес: {url}\n")

        # --- самопроверка снаружи -------------------------------------------
        hook = f"{url}/webhook/bothelp"
        bad = httpx.post(
            hook,
            json={"phone": "+70000000000", "step_id": "selftest"},
            headers={"X-Voronka-Token": "wrong"},
            timeout=20,
        )
        empty = httpx.post(
            hook,
            json={"step_id": "selftest"},  # без контакта -> 422, в CRM ничего не пишется
            headers={"X-Voronka-Token": s.webhook_token},
            timeout=20,
        )
        print("самопроверка через тоннель:")
        print(f"  неверный токен  -> {bad.status_code} (ожидалось 401)")
        print(f"  без контакта    -> {empty.status_code} (ожидалось 422)")
        if bad.status_code != 401 or empty.status_code != 422:
            print("  ВНИМАНИЕ: ответы не те, что ожидались — проверьте конфиг перед BotHelp")
        else:
            print("  всё сходится, снаружи отвечает наш конвейер\n")

        body = {
            "phone": "{{phone}}",
            "telegram_id": "{{telegram_id}}",
            "qualified": True,
            "budget": "{{budget}}",
            "timeline": "{{timeline}}",
            "segment": "{{segment}}",
            "transcript": "Задача: {{task}}\\nБюджет: {{budget}}\\nСроки: {{timeline}}",
            "step_id": "qualification",
        }
        print("=" * 70)
        print("НАСТРОЙКИ ДЛЯ ДЕЙСТВИЯ «ВНЕШНИЙ ЗАПРОС» В BOTHELP")
        print("=" * 70)
        print(f"Метод:  POST")
        print(f"URL:    {hook}")
        print("Заголовки:")
        print("        Content-Type: application/json")
        print(f"        X-Voronka-Token: {s.webhook_token}")
        print("Тело:")
        print(json.dumps(body, ensure_ascii=False, indent=2))
        print("Маппинг ответа (JSON Path -> поле подписчика):")
        print("        $.lead_id  ->  amo_lead_id")
        print("        $.status   ->  last_sync_status")
        print("=" * 70)
        print(f"\nжурнал событий: http://127.0.0.1:{PORT}/journal")
        print("инспектор запросов ngrok: http://127.0.0.1:4040")
        if "--once" in sys.argv:
            print("\n--once: проверка пройдена, останавливаюсь.")
            return
        print("\nCtrl+C — остановить конвейер и тоннель.")

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nостанавливаю…")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
