"""Manager-chat notifications: HTML, 4096-char splitting, retries and dedupe."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import AiogramError
from aiogram.types import LinkPreviewOptions

from tg_site_login.events import NotificationEvent
from tg_site_login.htmlfmt import TELEGRAM_TEXT_LIMIT, split_html
from tg_site_login.retry import RetryPolicy, Sleep, call_with_retry
from tg_site_login.store import Store

logger = logging.getLogger(__name__)


class NotifyResult(StrEnum):
    SENT = "sent"
    DISABLED = "disabled"  # kill switch is on
    DUPLICATE = "duplicate"  # this event was already delivered
    IN_PROGRESS = "in_progress"  # another sender holds the event right now


class NotificationError(RuntimeError):
    """Delivery failed for good. The dedupe reservation is released, so the caller may retry."""

    def __init__(self, dedupe_key: str, cause: BaseException) -> None:
        super().__init__(f"could not deliver {dedupe_key}: {type(cause).__name__}")
        self.dedupe_key = dedupe_key
        self.__cause__ = cause


class Notifier:
    """Send events to one chat.

    Delivery guarantees:

    * **Idempotent per event.** The event's ``dedupe_key`` is checked in the ``Store``; once
      an event is delivered, further ``notify`` calls with the same key do nothing.
    * **Resumable.** A message split into several parts remembers how many parts were
      delivered, so a retry after a failure continues with the missing part only.
    * **No concurrent senders.** A short-lived lock keeps two workers from sending the
      same event at once.
    * **At least once on ambiguity.** If a request times out after it was sent, Telegram
      may have delivered it; the retry then sends it again. A duplicate ping to a manager
      is preferred over a lost one.
    """

    def __init__(
        self,
        bot: Bot,
        chat_id: int | str,
        store: Store,
        *,
        policy: RetryPolicy | None = None,
        is_enabled: Callable[[], bool] = lambda: True,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        min_interval: float = 1.0,
        dedupe_ttl: float = 7 * 24 * 3600,
        lock_ttl: float = 300.0,
        send_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._store = store
        self._policy = policy or RetryPolicy()
        self._is_enabled = is_enabled
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._clock = clock
        self._min_interval = min_interval
        self._dedupe_ttl = dedupe_ttl
        self._lock_ttl = lock_ttl
        self._send_kwargs = send_kwargs or {}
        self._pace_lock = asyncio.Lock()
        self._last_sent_at: float | None = None

    async def notify(self, event: NotificationEvent) -> NotifyResult:
        """Deliver ``event`` unless it is disabled, already delivered or in flight."""
        if not self._is_enabled():
            logger.info("notification skipped, kill switch is on: %s", event.dedupe_key)
            return NotifyResult.DISABLED

        key = event.dedupe_key
        done_key = f"notify:done:{key}"
        if await self._store.get(done_key) is not None:
            return NotifyResult.DUPLICATE
        lock_key = f"notify:lock:{key}"
        if not await self._store.set(lock_key, "1", self._lock_ttl, nx=True):
            return NotifyResult.IN_PROGRESS
        try:
            if await self._store.get(done_key) is not None:  # finished while we took the lock
                return NotifyResult.DUPLICATE
            parts = split_html(event.render(), TELEGRAM_TEXT_LIMIT)
            progress_key = f"notify:progress:{key}"
            sent = int(await self._store.get(progress_key) or 0)
            for index in range(sent, len(parts)):
                await self._send_part(parts[index], key)
                await self._store.set(progress_key, str(index + 1), self._dedupe_ttl)
            await self._store.set(done_key, "1", self._dedupe_ttl)
            await self._store.delete(progress_key)
            return NotifyResult.SENT
        finally:
            await self._store.delete(lock_key)

    async def _send_part(self, text: str, key: str) -> None:
        await self._pace()
        try:
            await call_with_retry(
                lambda: self._bot.send_message(
                    self._chat_id,
                    text,
                    parse_mode=ParseMode.HTML,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    request_timeout=int(self._policy.request_timeout) + 1,
                    **self._send_kwargs,
                ),
                policy=self._policy,
                sleep=self._sleep,
                rng=self._rng,
                label=f"notify {key}",
            )
        except (AiogramError, TimeoutError) as exc:
            raise NotificationError(key, exc) from exc

    async def _pace(self) -> None:
        """Keep at least ``min_interval`` seconds between sends from this process."""
        if self._min_interval <= 0:
            return
        async with self._pace_lock:
            if self._last_sent_at is not None:
                wait = self._min_interval - (self._clock() - self._last_sent_at)
                if wait > 0:
                    await self._sleep(wait)
            self._last_sent_at = self._clock()
