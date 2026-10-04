from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from decimal import Decimal

import pytest
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import GetMe
from aiogram.types import LinkPreviewOptions

from tests.support import CHAT_ID, FakeClock, FakeSession, RecordingSleep, make_bot
from tg_site_login.events import (
    LeadCreated,
    NotificationEvent,
    OrderCreated,
    OrderItem,
    OrderUnpaid,
    PlainTextEvent,
    format_money,
)
from tg_site_login.htmlfmt import TELEGRAM_TEXT_LIMIT, split_html, visible_length
from tg_site_login.notifier import NotificationError, Notifier, NotifyResult
from tg_site_login.retry import RetryPolicy
from tg_site_login.store import MemoryStore

METHOD = GetMe()


def net() -> TelegramNetworkError:
    return TelegramNetworkError(method=METHOD, message="connection reset")


def make_notifier(
    store: MemoryStore,
    session: FakeSession | None = None,
    *,
    policy: RetryPolicy | None = None,
    min_interval: float = 0.0,
    is_enabled: Callable[[], bool] = lambda: True,
    clock: Callable[[], float] = time.monotonic,
    lock_ttl: float = 300.0,
) -> tuple[Notifier, FakeSession, RecordingSleep]:
    bot, fake = make_bot(session)
    sleep = RecordingSleep()
    notifier = Notifier(
        bot,
        CHAT_ID,
        store,
        policy=policy or RetryPolicy(max_attempts=4, base_delay=1.0, jitter=0.5, request_timeout=5),
        sleep=sleep,
        rng=random.Random(7),
        clock=clock,
        min_interval=min_interval,
        is_enabled=is_enabled,
        lock_ttl=lock_ttl,
    )
    return notifier, fake, sleep


def order(
    order_id: str = "1001",
    *,
    customer_name: str = "Anna",
    items: tuple[OrderItem, ...] = (OrderItem("T-shirt", 2, Decimal("990")),),
    phone: str | None = "+79991234567",
    payment_method: str | None = None,
    comment: str | None = None,
) -> OrderCreated:
    return OrderCreated(
        order_id=order_id,
        customer_name=customer_name,
        items=items,
        phone=phone,
        payment_method=payment_method,
        comment=comment,
    )


# -- formatting -----------------------------------------------------------------------------


async def test_order_message_is_html_and_sent_to_the_manager_chat(store: MemoryStore) -> None:
    notifier, session, _ = make_notifier(store)

    assert await notifier.notify(order()) is NotifyResult.SENT

    (sent,) = session.delivered
    assert sent.chat_id == CHAT_ID
    assert sent.parse_mode == "HTML"
    assert isinstance(sent.link_preview_options, LinkPreviewOptions)
    assert sent.link_preview_options.is_disabled is True
    assert "<b>Новый заказ №1001</b>" in (sent.text or "")
    assert "T-shirt, 2 шт. — 1 980 ₽" in (sent.text or "")
    assert "Итого: <b>1 980 ₽</b>" in (sent.text or "")
    assert "<code>+79991234567</code>" in (sent.text or "")


async def test_user_input_is_escaped_in_every_event(store: MemoryStore) -> None:
    notifier, session, _ = make_notifier(store)
    evil = '<b onclick="x">&</b>'
    events: list[NotificationEvent] = [
        order("1", customer_name=evil, comment=evil, payment_method=evil),
        order("2", items=(OrderItem(evil, 1, Decimal(1)),), phone=evil),
        LeadCreated("3", name=evil, phone=evil, topic=evil, message=evil, source=evil),
        OrderUnpaid("4", customer_name=evil, total=Decimal(5), waiting_minutes=31),
        PlainTextEvent("5", evil),
    ]
    for event in events:
        await notifier.notify(event)

    assert len(session.delivered) == 5
    for sent in session.delivered:
        text = sent.text or ""
        assert "&lt;b onclick" in text
        assert "<b onclick" not in text
        assert "&amp;&lt;/b&gt;" in text


async def test_lead_and_unpaid_templates(store: MemoryStore) -> None:
    notifier, session, _ = make_notifier(store)
    await notifier.notify(
        LeadCreated("L-1", name="Boris", phone="+79990000002", topic="Restock", message="Hi")
    )
    await notifier.notify(
        OrderUnpaid("1001", customer_name="Anna", total=Decimal("1980.50"), waiting_minutes=30)
    )
    lead, unpaid = (m.text or "" for m in session.delivered)
    assert lead.startswith("<b>Новая заявка</b> (Restock)")
    assert "Сообщение: Hi" in lead
    assert unpaid.startswith("<b>Заказ не оплачен №1001</b>")
    assert "Ждёт оплаты уже 30 мин." in unpaid
    assert "Сумма: <b>1 980.50 ₽</b>" in unpaid


def test_money_formatting() -> None:
    assert format_money(Decimal("1234567.5"), "RUB") == "1 234 567.50 ₽"
    assert format_money(Decimal("10"), "USD") == "10 $"
    assert format_money(Decimal("10"), "KZT") == "10 KZT"


async def test_long_message_is_split_under_the_telegram_limit(store: MemoryStore) -> None:
    notifier, session, _ = make_notifier(store)
    items = tuple(OrderItem(f"Item {i} & co <x>", 1, Decimal(100 + i)) for i in range(300))

    assert await notifier.notify(order("big", items=items)) is NotifyResult.SENT

    assert len(session.delivered) >= 2
    texts = [m.text or "" for m in session.delivered]
    assert all(visible_length(t) <= TELEGRAM_TEXT_LIMIT for t in texts)
    assert sum(t.count("Item ") for t in texts) == 300  # no row lost or duplicated
    assert "Итого:" in texts[-1]
    assert all("<x>" not in t for t in texts)


# -- retries --------------------------------------------------------------------------------


async def test_network_errors_are_retried_with_backoff(store: MemoryStore) -> None:
    session = FakeSession()
    session.fail_next(net(), net())
    notifier, session, sleep = make_notifier(store, session)

    assert await notifier.notify(order()) is NotifyResult.SENT

    assert len(session.requests) == 3
    assert len(session.delivered) == 1
    assert len(sleep.delays) == 2
    assert 0.5 <= sleep.delays[0] <= 1.0
    assert 1.0 <= sleep.delays[1] <= 2.0


async def test_429_waits_for_retry_after(store: MemoryStore) -> None:
    session = FakeSession()
    session.fail_next(TelegramRetryAfter(method=METHOD, message="Too Many Requests", retry_after=9))
    notifier, session, sleep = make_notifier(store, session)

    assert await notifier.notify(order()) is NotifyResult.SENT
    assert len(sleep.delays) == 1
    assert 9 <= sleep.delays[0] <= 10


async def test_hung_request_is_timed_out_and_retried(store: MemoryStore) -> None:
    session = FakeSession()
    session.fail_next("hang")
    notifier, session, sleep = make_notifier(
        store, session, policy=RetryPolicy(max_attempts=3, request_timeout=0.05)
    )
    assert await notifier.notify(order()) is NotifyResult.SENT
    assert len(session.requests) == 2
    assert len(sleep.delays) == 1


async def test_permanent_error_is_not_retried_and_can_be_retried_later(
    store: MemoryStore,
) -> None:
    session = FakeSession()
    session.fail_next(TelegramForbiddenError(method=METHOD, message="bot was kicked"))
    notifier, session, sleep = make_notifier(store, session)

    with pytest.raises(NotificationError) as caught:
        await notifier.notify(order())
    assert isinstance(caught.value.__cause__, TelegramForbiddenError)
    assert caught.value.dedupe_key == "order-created:1001"
    assert len(session.requests) == 1
    assert sleep.delays == []

    # the failure left no "done" marker and no lock behind, so a later retry works
    assert await notifier.notify(order()) is NotifyResult.SENT
    assert len(session.delivered) == 1


async def test_exhausted_retries_raise_notification_error(store: MemoryStore) -> None:
    session = FakeSession()
    session.fail_next(*(net() for _ in range(10)))
    notifier, session, _ = make_notifier(store, session)
    with pytest.raises(NotificationError, match="order-created:1001"):
        await notifier.notify(order())
    assert len(session.requests) == 4  # max_attempts
    assert session.delivered == []


# -- dedupe ---------------------------------------------------------------------------------


async def test_the_same_event_is_never_delivered_twice(store: MemoryStore) -> None:
    notifier, session, _ = make_notifier(store)
    assert await notifier.notify(order()) is NotifyResult.SENT
    assert await notifier.notify(order()) is NotifyResult.DUPLICATE
    assert await notifier.notify(order()) is NotifyResult.DUPLICATE
    assert len(session.delivered) == 1
    # different events still go through
    assert await notifier.notify(order("1002")) is NotifyResult.SENT
    assert len(session.delivered) == 2


async def test_caller_retry_after_a_failure_does_not_double_send_delivered_parts(
    store: MemoryStore,
) -> None:
    """A multi-part message that failed halfway resumes at the missing part."""
    items = tuple(OrderItem(f"Item {i}", 1, Decimal(100)) for i in range(400))
    event = order("big", items=items)
    total_parts = len(split_html(event.render()))
    assert total_parts >= 3

    session = FakeSession()
    # part 1 is delivered, then every attempt for part 2 (max_attempts=4) fails
    session.fail_next(None, net(), net(), net(), net())
    notifier, session, _ = make_notifier(store, session)

    with pytest.raises(NotificationError):
        await notifier.notify(event)
    assert len(session.delivered) == 1

    # the network recovers and the caller retries the whole event
    assert await notifier.notify(event) is NotifyResult.SENT
    texts = [m.text or "" for m in session.delivered]
    assert len(texts) == total_parts  # every part exactly once
    assert len(set(texts)) == total_parts
    assert sum(t.count("Item ") for t in texts) == 400
    assert await notifier.notify(event) is NotifyResult.DUPLICATE


async def test_concurrent_notifications_of_one_event_send_once(store: MemoryStore) -> None:
    session = FakeSession()
    session.yield_control = True
    notifier, session, _ = make_notifier(store, session)

    results = await asyncio.gather(*(notifier.notify(order()) for _ in range(5)))

    assert results.count(NotifyResult.SENT) == 1
    assert set(results) <= {NotifyResult.SENT, NotifyResult.IN_PROGRESS, NotifyResult.DUPLICATE}
    assert len(session.delivered) == 1


async def test_event_held_by_another_sender_is_reported_in_progress(store: MemoryStore) -> None:
    notifier, session, _ = make_notifier(store)
    await store.set("notify:lock:order-created:1001", "1", 60)
    assert await notifier.notify(order()) is NotifyResult.IN_PROGRESS
    assert session.requests == []


async def test_a_delivered_event_is_a_duplicate_even_while_a_lock_is_present(
    store: MemoryStore,
) -> None:
    notifier, session, _ = make_notifier(store)
    await notifier.notify(order())
    await store.set("notify:lock:order-created:1001", "1", 60)  # a late retry holds the lock
    assert await notifier.notify(order()) is NotifyResult.DUPLICATE
    assert len(session.delivered) == 1


async def test_stale_lock_expires_so_a_crashed_sender_does_not_block_forever(
    store: MemoryStore, clock: FakeClock
) -> None:
    notifier, session, _ = make_notifier(store, lock_ttl=30)
    await store.set("notify:lock:order-created:1001", "1", 30)
    clock.advance(31)
    assert await notifier.notify(order()) is NotifyResult.SENT
    assert len(session.delivered) == 1


# -- kill switch and pacing -----------------------------------------------------------------


async def test_kill_switch_sends_nothing_and_is_checked_on_every_call(
    store: MemoryStore,
) -> None:
    enabled = {"on": False}
    notifier, session, _ = make_notifier(store, is_enabled=lambda: enabled["on"])

    assert await notifier.notify(order()) is NotifyResult.DISABLED
    assert session.requests == []

    enabled["on"] = True  # flipping the switch back does not need a new Notifier
    assert await notifier.notify(order()) is NotifyResult.SENT
    assert len(session.delivered) == 1


async def test_disabled_events_are_not_marked_as_delivered(store: MemoryStore) -> None:
    enabled = {"on": False}
    notifier, _, _ = make_notifier(store, is_enabled=lambda: enabled["on"])
    await notifier.notify(order())
    enabled["on"] = True
    assert await notifier.notify(order()) is NotifyResult.SENT


async def test_sends_are_paced_per_chat(store: MemoryStore) -> None:
    items = tuple(OrderItem(f"Item {i}", 1, Decimal(100)) for i in range(400))
    notifier, session, sleep = make_notifier(store, min_interval=1.0, clock=lambda: 100.0)
    await notifier.notify(order("big", items=items))
    parts = len(session.delivered)
    assert parts >= 2
    assert sleep.delays == [1.0] * (parts - 1)  # first send is immediate, the rest wait
