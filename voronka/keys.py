"""Натуральный ключ заявки.

Идемпотентность строится не на request_id отправителя (его легко потерять или
сгенерировать заново), а на нормализованных контактных данных. Один и тот же
человек, приславший форму дважды, даёт один и тот же dedup_key.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

_NON_DIGIT = re.compile(r"\D+")


def normalize_phone(raw: str | None) -> str:
    """Приводит телефон к цифрам в формате страны.

    Правила подобраны под КЗ/РФ, где один и тот же номер пишут как
    +7 707 123-45-67, 8 707 123 45 67, 87071234567.
    Ведущая 8 при длине 11 заменяется на 7.
    """
    if not raw:
        return ""
    digits = _NON_DIGIT.sub("", raw)
    if not digits:
        return ""
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10 and digits.startswith("7"):
        # 7071234567 без кода страны -> считаем КЗ/РФ
        digits = "7" + digits
    return digits


def normalize_email(raw: str | None) -> str:
    if not raw:
        return ""
    value = unicodedata.normalize("NFKC", raw).strip().lower()
    if "@" not in value:
        return ""
    local, _, domain = value.partition("@")
    # Точки и +suffix в gmail не меняют адресата.
    if domain in {"gmail.com", "googlemail.com"}:
        local = local.split("+", 1)[0].replace(".", "")
        domain = "gmail.com"
    if not local or not domain:
        return ""
    return f"{local}@{domain}"


def normalize_telegram(raw: str | None) -> str:
    if not raw:
        return ""
    return raw.strip().lstrip("@").lower()


def dedup_key(
    *,
    source: str,
    phone: str | None = None,
    email: str | None = None,
    telegram_id: str | None = None,
    form_id: str | None = None,
) -> str:
    """Стабильный ключ заявки.

    Приоритет: телефон > email > telegram_id. Если ни одного контакта нет —
    вызывающий код обязан отдать 422, «заявка без контакта» неотличима от любой
    другой такой же и дедуп на ней невозможен.
    """
    phone_n = normalize_phone(phone)
    email_n = normalize_email(email)
    tg_n = normalize_telegram(telegram_id)

    if phone_n:
        ident = f"phone:{phone_n}"
    elif email_n:
        ident = f"email:{email_n}"
    elif tg_n:
        ident = f"tg:{tg_n}"
    else:
        raise ValueError("no contact identity: phone, email and telegram_id are empty")

    scope = f"{source}|{form_id or '-'}"
    raw = f"{scope}|{ident}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
