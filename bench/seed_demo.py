"""Наполняет запущенный стенд осмысленными событиями — для скриншотов и демо.

    python serve.py            # в одном терминале
    python -m bench.seed_demo  # в другом

Сценарий повторяет то, что происходит в жизни: обычная заявка, её повтор,
диалог бота, неисправимая ошибка CRM и короткая авария.
"""
from __future__ import annotations

import time

import httpx

API = "http://127.0.0.1:8080"
MOCK = "http://127.0.0.1:8081"


def run(api: str = API, mock: str = MOCK, verbose: bool = True) -> dict:
    def say(*args) -> None:
        if verbose:
            print(*args, flush=True)

    def post(path: str, payload: dict) -> dict:
        return httpx.post(f"{api}{path}", json=payload, timeout=15).json()

    def wait_quiet(timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if httpx.get(f"{api}/api/stats", timeout=10).json()["pending"] == 0:
                return
            time.sleep(0.3)

    def fault(mode: str, seconds: float) -> None:
        httpx.post(
            f"{mock}/_control/fault",
            json={"mode": mode, "seconds": seconds, "probability": 1.0},
            timeout=10,
        )

    say("1. обычная заявка с сайта")
    say("  ", post("/webhook/form", {
        "name": "Айгерим Сатпаева",
        "phone": "+7 701 774 09 12",
        "email": "aigerim@kaspi-shop.kz",
        "comment": "Нужна автоворонка в Telegram и выгрузка заявок в amoCRM",
        "form_id": "landing",
        "utm_source": "instagram",
    }))
    wait_quiet()

    say("2. тот же человек отправил форму повторно (телефон в другом формате)")
    say("  ", post("/webhook/form", {
        "name": "Айгерим",
        "phone": "8 701 774 09 12",
        "email": "AIGERIM@Kaspi-Shop.kz",
        "form_id": "landing",
        "request_id": "retry-from-crm-webhook",
    }))

    say("3. итог диалога в BotHelp — сделка обновляется, а не создаётся заново")
    say("  ", post("/webhook/bothelp", {
        "phone": "+77017740912",
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
    }))
    wait_quiet()

    say("4. повтор того же шага бота — отсекается, второго примечания не будет")
    say("  ", post("/webhook/bothelp", {
        "phone": "+77017740912",
        "qualified": True,
        "budget": "300–500 тыс ₸",
        "step_id": "qualification",
    }))

    say("5. заявка без контакта — отклоняется на входе")
    say("  ", post("/webhook/form", {"name": "Аноним", "comment": "просто посмотреть"}))

    say("6. неисправимая ошибка CRM (400) — сразу в DLQ, попытки не жжём")
    fault("http_400", 4)
    say("  ", post("/webhook/form", {
        "name": "Данияр Ким",
        "phone": "+7 705 300 11 22",
        "comment": "Заявка попала на кривой маппинг полей",
    }))
    wait_quiet()
    fault("off", 0)

    say("7. короткая авария CRM (503) — заявка переживает её на ретраях")
    fault("http_503", 6)
    say("  ", post("/webhook/form", {
        "name": "Ольга Пак",
        "phone": "+7 747 909 55 41",
        "email": "olga@tabys.kz",
        "comment": "Заявка пришла ровно в момент падения amoCRM",
    }))
    time.sleep(7)
    wait_quiet()

    stats = httpx.get(f"{api}/api/stats", timeout=10).json()
    crm = httpx.get(f"{mock}/_control/state", timeout=10).json()
    say("\nЖурнал:", stats["journal"])
    say("Очередь:", stats["queue"])
    say("В CRM:", f"сделок {crm['leads']}, контактов {crm['contacts']}, примечаний {crm['notes']}")
    say(f"\nОткройте {api}/journal")
    return {"stats": stats, "crm": crm}


if __name__ == "__main__":
    run()
