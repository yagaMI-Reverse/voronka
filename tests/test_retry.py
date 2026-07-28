import random

import pytest

from voronka.retry import PermanentError, RetryableError, backoff_delay, classify_status


def test_backoff_is_exponential_without_jitter():
    delays = [backoff_delay(a, base=1.0, cap=1000, jitter=False) for a in range(1, 6)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_backoff_respects_cap():
    assert backoff_delay(10, base=1.0, cap=60.0, jitter=False) == 60.0


def test_full_jitter_stays_in_range():
    rng = random.Random(7)
    for attempt in range(1, 8):
        ceiling = min(60.0, 1.0 * 2 ** (attempt - 1))
        for _ in range(50):
            d = backoff_delay(attempt, base=1.0, cap=60.0, jitter=True, rng=rng)
            assert 0.0 <= d <= ceiling


def test_attempt_must_be_positive():
    with pytest.raises(ValueError):
        backoff_delay(0)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 408])
def test_transient_statuses_are_retryable(status):
    assert classify_status(status) is RetryableError


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_permanent(status):
    assert classify_status(status) is PermanentError


@pytest.mark.parametrize("status", [200, 201, 202, 204])
def test_success_is_not_an_error(status):
    assert classify_status(status) is None
