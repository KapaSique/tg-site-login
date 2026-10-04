"""Fixed-window rate limiter on top of ``Store.incr``."""

from __future__ import annotations

from dataclasses import dataclass

from tg_site_login.store import Store


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    retry_after: int  # seconds until the window resets (upper bound)


class RateLimiter:
    """Allow at most ``limit`` hits per ``window`` seconds for each subject."""

    def __init__(self, store: Store, name: str, *, limit: int, window: float) -> None:
        if limit < 1 or window <= 0:
            msg = "limit must be >= 1 and window must be > 0"
            raise ValueError(msg)
        self._store = store
        self._name = name
        self._limit = limit
        self._window = window

    async def hit(self, subject: str) -> RateLimitResult:
        count = await self._store.incr(f"rl:{self._name}:{subject}", self._window)
        return RateLimitResult(
            allowed=count <= self._limit,
            retry_after=int(self._window) if self._window >= 1 else 1,
        )
