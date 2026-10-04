"""Reminders for orders that stay unpaid, built on top of the ``Notifier``."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from tg_site_login.events import OrderUnpaid
from tg_site_login.notifier import NotificationError, Notifier, NotifyResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PendingOrder:
    order_id: str
    customer_name: str
    total: Decimal
    created_at: datetime
    currency: str = "RUB"
    phone: str | None = None
    payment_method: str | None = None


class OrderSource(Protocol):
    """Where the reminder job reads orders from (your database or shop API)."""

    async def list_unpaid(
        self, *, older_than: datetime, newer_than: datetime
    ) -> Sequence[PendingOrder]:
        """Orders still awaiting payment that were created in ``(newer_than, older_than]``."""
        ...

    async def is_unpaid(self, order_id: str) -> bool:
        """Fresh check right before sending; the order may have been paid meanwhile."""
        ...


class UnpaidOrderReminder:
    """Send one "order unpaid" message per order after it waited ``min_age``.

    The window ``[min_age, max_age]`` keeps the job from nagging about stale orders after
    a long outage. The per-order dedupe key makes the reminder idempotent, so the job may
    run as often as you like and from several workers.
    """

    def __init__(
        self,
        source: OrderSource,
        notifier: Notifier,
        *,
        min_age: timedelta = timedelta(minutes=30),
        max_age: timedelta = timedelta(hours=24),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if min_age >= max_age:
            msg = "min_age must be shorter than max_age"
            raise ValueError(msg)
        self._source = source
        self._notifier = notifier
        self._min_age = min_age
        self._max_age = max_age
        self._now = now

    async def run_once(self) -> dict[str, NotifyResult]:
        """One pass; returns the outcome per order id."""
        now = self._now()
        orders = await self._source.list_unpaid(
            older_than=now - self._min_age, newer_than=now - self._max_age
        )
        outcomes: dict[str, NotifyResult] = {}
        for order in orders:
            if not await self._source.is_unpaid(order.order_id):
                continue
            event = OrderUnpaid(
                order_id=order.order_id,
                customer_name=order.customer_name,
                total=order.total,
                waiting_minutes=int((now - order.created_at).total_seconds() // 60),
                currency=order.currency,
                phone=order.phone,
                payment_method=order.payment_method,
            )
            try:
                outcomes[order.order_id] = await self._notifier.notify(event)
            except NotificationError:
                logger.exception("unpaid reminder failed for order %s", order.order_id)
        return outcomes

    async def run_forever(self, interval: float = 300.0) -> None:
        """Run ``run_once`` every ``interval`` seconds until cancelled."""
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("unpaid reminder pass failed")
            await asyncio.sleep(interval)
