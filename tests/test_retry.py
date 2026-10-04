from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import pytest
from aiogram.exceptions import (
    ClientDecodeError,
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetMe, SendMessage

from tests.support import RecordingSleep, make_bot
from tg_site_login.retry import RetryPolicy, call_with_retry

METHOD = GetMe()


def network_error(message: str = "boom") -> TelegramNetworkError:
    return TelegramNetworkError(method=METHOD, message=message)


class Flaky:
    """Callable that raises the queued errors one by one, then returns ``"ok"``."""

    def __init__(self, *errors: BaseException) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


async def test_success_needs_no_sleep() -> None:
    sleep = RecordingSleep()
    assert await call_with_retry(Flaky(), policy=RetryPolicy(), sleep=sleep) == "ok"
    assert sleep.delays == []


async def test_network_errors_are_retried_with_exponential_backoff() -> None:
    sleep = RecordingSleep()
    flaky = Flaky(network_error(), network_error(), network_error())
    policy = RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=100.0, jitter=0.5)

    assert await call_with_retry(flaky, policy=policy, sleep=sleep, rng=random.Random(1)) == "ok"

    assert flaky.calls == 4
    assert len(sleep.delays) == 3
    for attempt, delay in enumerate(sleep.delays, start=1):
        ceiling = 2 ** (attempt - 1)  # 1, 2, 4
        assert ceiling * 0.5 <= delay <= ceiling  # equal jitter: half fixed, half random
    assert sleep.delays[0] < sleep.delays[2]  # the delays grow


async def test_backoff_is_capped_by_max_delay() -> None:
    policy = RetryPolicy(base_delay=1.0, max_delay=5.0, jitter=0.0)
    rng = random.Random(0)
    assert [policy.backoff(n, rng) for n in (1, 2, 3, 4, 10, 80)] == [1, 2, 4, 5, 5, 5]


async def test_jitter_spreads_delays() -> None:
    policy = RetryPolicy(base_delay=1.0, max_delay=100.0, jitter=1.0)
    rng = random.Random(42)
    delays = {round(policy.backoff(3, rng), 6) for _ in range(20)}
    assert len(delays) > 10
    assert all(0 <= d <= 4 for d in delays)


async def test_retry_after_is_honoured() -> None:
    sleep = RecordingSleep()
    flaky = Flaky(TelegramRetryAfter(method=METHOD, message="Too Many Requests", retry_after=7))
    policy = RetryPolicy(retry_after_jitter=1.0)

    assert await call_with_retry(flaky, policy=policy, sleep=sleep, rng=random.Random(3)) == "ok"

    assert len(sleep.delays) == 1
    assert 7 <= sleep.delays[0] <= 8  # exactly what Telegram asked, plus a little jitter


async def test_retry_after_longer_than_the_cap_fails_fast() -> None:
    sleep = RecordingSleep()
    flaky = Flaky(TelegramRetryAfter(method=METHOD, message="flood", retry_after=600))
    with pytest.raises(TelegramRetryAfter):
        await call_with_retry(flaky, policy=RetryPolicy(max_retry_after=60), sleep=sleep)
    assert flaky.calls == 1
    assert sleep.delays == []


async def test_429_from_the_wire_is_parsed_into_retry_after() -> None:
    """The real Bot API reply shape maps to ``TelegramRetryAfter.retry_after``."""
    bot, session = make_bot()
    body: dict[str, Any] = {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests: retry after 5",
        "parameters": {"retry_after": 5},
    }
    method = SendMessage(chat_id=1, text="x")
    with pytest.raises(TelegramRetryAfter) as caught:
        session.check_response(bot=bot, method=method, status_code=429, content=json.dumps(body))
    assert caught.value.retry_after == 5


@pytest.mark.parametrize(
    "error",
    [
        TelegramBadRequest(method=METHOD, message="can't parse entities"),
        TelegramForbiddenError(method=METHOD, message="bot was blocked by the user"),
        TelegramUnauthorizedError(method=METHOD, message="Unauthorized"),
        TelegramEntityTooLarge(method=METHOD, message="Request Entity Too Large"),
    ],
)
async def test_permanent_errors_are_not_retried(error: Exception) -> None:
    sleep = RecordingSleep()
    flaky = Flaky(error)
    with pytest.raises(type(error)):
        await call_with_retry(flaky, policy=RetryPolicy(), sleep=sleep)
    assert flaky.calls == 1
    assert sleep.delays == []


@pytest.mark.parametrize(
    "error",
    [
        TelegramServerError(method=METHOD, message="Bad Gateway"),
        ClientDecodeError("not json", ValueError("x"), "<html>"),
        TimeoutError(),
    ],
)
async def test_server_errors_and_garbage_replies_are_retried(error: Exception) -> None:
    flaky = Flaky(error)
    assert await call_with_retry(flaky, policy=RetryPolicy(), sleep=RecordingSleep()) == "ok"
    assert flaky.calls == 2


async def test_gives_up_after_max_attempts_and_raises_the_last_error() -> None:
    sleep = RecordingSleep()
    flaky = Flaky(*(network_error(f"try {n}") for n in range(10)))
    with pytest.raises(TelegramNetworkError, match="try 2"):
        await call_with_retry(flaky, policy=RetryPolicy(max_attempts=3), sleep=sleep)
    assert flaky.calls == 3
    assert len(sleep.delays) == 2  # no sleep after the final attempt


async def test_hung_connection_is_cut_by_the_attempt_timeout() -> None:
    """A TCP handshake that never completes must not block the caller."""
    calls = 0

    async def hang_then_answer() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.Event().wait()
        return "ok"

    sleep = RecordingSleep()
    policy = RetryPolicy(request_timeout=0.05)
    assert await call_with_retry(hang_then_answer, policy=policy, sleep=sleep) == "ok"
    assert calls == 2
    assert len(sleep.delays) == 1


def test_policy_validation() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="jitter"):
        RetryPolicy(jitter=1.5)
