"""Timeouts, exponential backoff with jitter and ``retry_after`` handling for Bot API calls."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiogram.exceptions import (
    ClientDecodeError,
    TelegramAPIError,
    TelegramEntityTooLarge,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How hard to try before giving up on one Bot API call.

    ``request_timeout`` bounds every single attempt. A hung TCP handshake to the Bot API
    is then cut after a few seconds and retried, instead of blocking for minutes.
    """

    max_attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 30.0
    jitter: float = 0.5  # fraction of the delay that is randomized, 0 = none, 1 = full jitter
    request_timeout: float = 15.0
    max_retry_after: float = 120.0  # do not sleep longer than this for a 429, fail instead
    retry_after_jitter: float = 1.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            msg = "max_attempts must be >= 1"
            raise ValueError(msg)
        if not 0 <= self.jitter <= 1:
            msg = "jitter must be between 0 and 1"
            raise ValueError(msg)

    def backoff(self, attempt: int, rng: random.Random) -> float:
        """Delay before retry number ``attempt`` (1-based): ``base * 2**(attempt-1)``, capped."""
        ceiling = min(self.max_delay, self.base_delay * (1 << min(attempt - 1, 30)))
        return ceiling * (1 - self.jitter) + rng.uniform(0, ceiling * self.jitter)


_TRANSIENT = (TelegramNetworkError, TelegramServerError, ClientDecodeError, TimeoutError)


def _is_transient(exc: BaseException) -> bool:
    # A 413 is modelled as a network error by aiogram but retrying it never helps.
    return isinstance(exc, _TRANSIENT) and not isinstance(exc, TelegramEntityTooLarge)


async def call_with_retry[T](
    call: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
    label: str = "telegram call",
) -> T:
    """Run ``call`` with a per-attempt timeout, retrying transient failures.

    * network errors, timeouts and 5xx are retried with exponential backoff and jitter;
    * ``429`` waits for Telegram's ``retry_after`` (plus a little jitter) and is retried,
      unless it asks for more than ``policy.max_retry_after``;
    * everything else (bad request, forbidden, unauthorized, ...) is raised immediately.

    The last error is re-raised once attempts are exhausted.

    Note for callers: a timeout after the request was sent does not tell whether Telegram
    processed it, so a retried send can occasionally be delivered twice.
    """
    generator = rng or random.Random()
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await asyncio.wait_for(call(), timeout=policy.request_timeout)
        except TelegramRetryAfter as exc:
            if attempt == policy.max_attempts or exc.retry_after > policy.max_retry_after:
                raise
            delay = exc.retry_after + generator.uniform(0, policy.retry_after_jitter)
            logger.warning("%s: flood control, waiting %.1fs", label, delay)
        except TelegramAPIError as exc:
            if not _is_transient(exc) or attempt == policy.max_attempts:
                raise
            delay = policy.backoff(attempt, generator)
            logger.warning("%s: %s, retry in %.1fs", label, type(exc).__name__, delay)
        except (ClientDecodeError, TimeoutError) as exc:
            if attempt == policy.max_attempts:
                raise
            delay = policy.backoff(attempt, generator)
            logger.warning("%s: %s, retry in %.1fs", label, type(exc).__name__, delay)
        await sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
