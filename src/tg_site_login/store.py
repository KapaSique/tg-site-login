"""Key-value storage abstraction with TTLs, plus an in-memory implementation."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class Store(Protocol):
    """Minimal atomic key-value operations the library needs.

    Every operation below is a single atomic step in Redis, which is what makes
    one-time codes, dedupe locks and rate limits safe across several processes.
    """

    async def get(self, key: str) -> str | None:
        """Return the value or ``None`` if the key is missing or expired."""
        ...

    async def set(self, key: str, value: str, ttl: float, *, nx: bool = False) -> bool:
        """Store ``value`` for ``ttl`` seconds.

        With ``nx=True`` the write happens only if the key does not exist; the return
        value tells whether it happened. Without ``nx`` it always returns ``True``.
        """
        ...

    async def get_and_delete(self, key: str) -> str | None:
        """Atomically read and remove a key; only one concurrent caller gets the value."""
        ...

    async def delete(self, key: str) -> None:
        """Remove a key; missing keys are ignored."""
        ...

    async def incr(self, key: str, ttl: float) -> int:
        """Increment a counter; ``ttl`` is applied when the counter is created."""
        ...

    async def aclose(self) -> None:
        """Release resources."""
        ...


class MemoryStore:
    """Process-local ``Store`` for tests, demos and single-process deployments.

    The clock is injectable so tests can move time without sleeping.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._data: dict[str, tuple[str, float]] = {}

    def _live(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at <= self._clock():
            del self._data[key]
            return None
        return value

    async def get(self, key: str) -> str | None:
        return self._live(key)

    async def set(self, key: str, value: str, ttl: float, *, nx: bool = False) -> bool:
        if nx and self._live(key) is not None:
            return False
        self._data[key] = (value, self._clock() + ttl)
        return True

    async def get_and_delete(self, key: str) -> str | None:
        value = self._live(key)
        if value is not None:
            del self._data[key]
        return value

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def incr(self, key: str, ttl: float) -> int:
        current = self._live(key)
        if current is None:
            self._data[key] = ("1", self._clock() + ttl)
            return 1
        expires_at = self._data[key][1]
        count = int(current) + 1
        self._data[key] = (str(count), expires_at)
        return count

    async def aclose(self) -> None:
        self._data.clear()
