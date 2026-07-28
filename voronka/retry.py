"""Backoff и классификация ошибок.

Классификация — половина дела: 500/502/503/504, 429 и сетевые таймауты
повторять осмысленно, а 400 «поле не существует» можно повторять хоть вечно,
результат не изменится. Такие уходят в DLQ сразу, без сжигания попыток.
"""
from __future__ import annotations

import random


class AmoError(Exception):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


class RetryableError(AmoError):
    """Временная неисправность: сеть, таймаут, 429, 5xx."""


class PermanentError(AmoError):
    """Ошибка запроса: 400/403/404/422. Повтор не поможет — в DLQ."""


RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504, 507, 509}


def classify_status(status: int) -> type[AmoError] | None:
    if 200 <= status < 300:
        return None
    if status in RETRYABLE_STATUSES:
        return RetryableError
    return PermanentError


def backoff_delay(
    attempt: int,
    *,
    base: float = 1.0,
    cap: float = 60.0,
    jitter: bool = True,
    rng: random.Random | None = None,
) -> float:
    """Экспоненциальный backoff с полным джиттером.

    attempt — номер уже сделанной неудачной попытки (1 = первая неудача).
    Без джиттера: base * 2^(attempt-1), обрезано по cap.
    С джиттером: равномерно из [0, вычисленной задержки] — чтобы пачка событий,
    упавшая одновременно, не ломилась обратно одновременно же.
    """
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    raw = min(cap, base * (2 ** (attempt - 1)))
    if not jitter:
        return raw
    r = rng or random
    return r.uniform(0.0, raw)
