from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.support import CHAT_ID, FakeSession, RecordingSleep, make_bot
from tg_site_login.notifier import Notifier, NotifyResult
from tg_site_login.reminders import OrderSource, PendingOrder, UnpaidOrderReminder
from tg_site_login.retry import RetryPolicy
from tg_site_login.store import MemoryStore

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


class FakeOrders:
    def __init__(self, *orders: PendingOrder) -> None:
        self.orders = list(orders)
        self.paid: set[str] = set()
        self.pay_before_recheck: set[str] = set()

    async def list_unpaid(
        self, *, older_than: datetime, newer_than: datetime
    ) -> Sequence[PendingOrder]:
        return [
            o
            for o in self.orders
            if o.order_id not in self.paid and newer_than < o.created_at <= older_than
        ]

    async def is_unpaid(self, order_id: str) -> bool:
        if order_id in self.pay_before_recheck:
            self.paid.add(order_id)
        return order_id not in self.paid


def pending(order_id: str, minutes_ago: int) -> PendingOrder:
    return PendingOrder(
        order_id=order_id,
        customer_name="Anna",
        total=Decimal("1980"),
        created_at=NOW - timedelta(minutes=minutes_ago),
        phone="+79991234567",
    )


def make_reminder(
    source: OrderSource, store: MemoryStore
) -> tuple[UnpaidOrderReminder, FakeSession]:
    bot, session = make_bot()
    notifier = Notifier(
        bot,
        CHAT_ID,
        store,
        policy=RetryPolicy(max_attempts=2),
        sleep=RecordingSleep(),
        rng=random.Random(1),
        min_interval=0,
    )
    return UnpaidOrderReminder(source, notifier, now=lambda: NOW), session


async def test_orders_unpaid_for_thirty_minutes_get_one_reminder(store: MemoryStore) -> None:
    source = FakeOrders(
        pending("fresh", 10),  # too early
        pending("due", 31),
        pending("due-2", 120),
        pending("stale", 25 * 60),  # older than a day: not nagged after an outage
    )
    reminder, session = make_reminder(source, store)

    assert await reminder.run_once() == {"due": NotifyResult.SENT, "due-2": NotifyResult.SENT}
    texts = [m.text or "" for m in session.delivered]
    assert any("№due" in t and "Ждёт оплаты уже 31 мин." in t for t in texts)
    assert any("№due-2" in t and "Ждёт оплаты уже 120 мин." in t for t in texts)

    # running again, even from another worker, never repeats a reminder
    assert await reminder.run_once() == {
        "due": NotifyResult.DUPLICATE,
        "due-2": NotifyResult.DUPLICATE,
    }
    assert len(session.delivered) == 2


async def test_order_paid_between_listing_and_sending_is_skipped(store: MemoryStore) -> None:
    source = FakeOrders(pending("race", 40))
    source.pay_before_recheck.add("race")
    reminder, session = make_reminder(source, store)

    assert await reminder.run_once() == {}
    assert session.requests == []


async def test_paid_orders_are_not_listed(store: MemoryStore) -> None:
    source = FakeOrders(pending("paid", 40))
    source.paid.add("paid")
    reminder, session = make_reminder(source, store)
    assert await reminder.run_once() == {}
    assert session.requests == []


async def test_failed_reminder_does_not_stop_the_others(store: MemoryStore) -> None:
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import GetMe

    source = FakeOrders(pending("a", 40), pending("b", 41))
    reminder, session = make_reminder(source, store)
    session.fail_next(TelegramForbiddenError(method=GetMe(), message="blocked"))

    outcomes = await reminder.run_once()
    assert outcomes == {"b": NotifyResult.SENT}  # "a" failed and will be retried next pass
    assert await reminder.run_once() == {"a": NotifyResult.SENT, "b": NotifyResult.DUPLICATE}


def test_window_must_make_sense(store: MemoryStore) -> None:
    bot, _ = make_bot()
    notifier = Notifier(bot, CHAT_ID, store)
    with pytest.raises(ValueError, match="min_age"):
        UnpaidOrderReminder(
            FakeOrders(), notifier, min_age=timedelta(hours=2), max_age=timedelta(hours=1)
        )
