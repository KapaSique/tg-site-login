"""Bot handlers driven through the real dispatcher with a fake Bot API session."""

from __future__ import annotations

import random

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetMe, SendMessage
from aiogram.types import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update

from tests.conftest import make_login
from tests.support import (
    FakeClock,
    RecordingSleep,
    contact_of,
    make_bot,
    private_message,
    user,
)
from tg_site_login.bot.handlers import build_router
from tg_site_login.bot.texts import Texts
from tg_site_login.directory import Customer, InMemoryPhoneDirectory
from tg_site_login.login import LoginService, PollStatus
from tg_site_login.retry import RetryPolicy
from tg_site_login.store import MemoryStore

ANNA = 111
EVE = 666
T = Texts()


class Harness:
    def __init__(self, login: LoginService, *, texts: Texts | None = None) -> None:
        self.login = login
        self.bot, self.session = make_bot()
        self.sleep = RecordingSleep()
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(
            build_router(
                login,
                site_name="shop.example",
                texts=texts,
                policy=RetryPolicy(max_attempts=3, base_delay=0.1),
                sleep=self.sleep,
                rng=random.Random(1),
            )
        )

    async def feed(self, update: Update) -> list[SendMessage]:
        before = len(self.session.delivered)
        await self.dispatcher.feed_update(self.bot, update)
        return self.session.delivered[before:]

    async def send_text(self, user_id: int, text: str) -> list[SendMessage]:
        return await self.feed(private_message(user_id, text=text))


@pytest.fixture
def harness(login: LoginService) -> Harness:
    return Harness(login)


def text_of(message: SendMessage) -> str:
    return message.text or ""


async def open_link(h: Harness, user_id: int = ANNA) -> tuple[str, list[SendMessage]]:
    started = await h.login.start()
    return started.nonce, await h.send_text(user_id, f"/start {started.code}")


# -- /start ---------------------------------------------------------------------------------


async def test_deep_link_asks_for_the_contact_with_a_request_contact_button(
    harness: Harness,
) -> None:
    started = await harness.login.start()
    (reply,) = await harness.send_text(ANNA, f"/start {started.code}")

    assert reply.chat_id == ANNA
    assert "shop.example" in text_of(reply)
    assert f"<b>{started.pairing_code}</b>" in text_of(reply)  # anti-phishing pairing code
    assert isinstance(reply.reply_markup, ReplyKeyboardMarkup)
    (button,) = reply.reply_markup.keyboard[0]
    assert button.request_contact is True
    assert button.text == T.share_button


async def test_plain_start_explains_where_to_log_in(harness: Harness) -> None:
    (reply,) = await harness.send_text(ANNA, "/start")
    assert text_of(reply) == T.welcome.format(site="shop.example")
    assert reply.reply_markup is None


@pytest.mark.parametrize("payload", ["garbage", "A" * 32, "x" * 100, "../../etc"])
async def test_unknown_or_malformed_code(harness: Harness, payload: str) -> None:
    (reply,) = await harness.send_text(ANNA, f"/start {payload}")
    assert text_of(reply) == T.invalid_code
    assert isinstance(reply.reply_markup, ReplyKeyboardRemove)


async def test_expired_code(harness: Harness, clock: FakeClock) -> None:
    started = await harness.login.start()
    clock.advance(301)
    (reply,) = await harness.send_text(ANNA, f"/start {started.code}")
    assert text_of(reply) == T.invalid_code


async def test_second_telegram_user_cannot_open_the_same_link(harness: Harness) -> None:
    started = await harness.login.start()
    await harness.send_text(ANNA, f"/start {started.code}")
    (reply,) = await harness.send_text(EVE, f"/start {started.code}")
    assert text_of(reply) == T.taken


async def test_repeated_bad_links_hit_the_rate_limit(
    store: MemoryStore, directory: InMemoryPhoneDirectory, clock: FakeClock
) -> None:
    harness = Harness(make_login(store, directory, clock, bad_code_limit=2))
    replies = [text_of((await harness.send_text(EVE, f"/start {'B' * 32}"))[0]) for _ in range(4)]
    assert replies == [T.invalid_code, T.invalid_code, T.rate_limited, T.rate_limited]


# -- contact --------------------------------------------------------------------------------


async def test_own_contact_confirms_the_login(harness: Harness) -> None:
    nonce, _ = await open_link(harness)

    (reply,) = await harness.feed(
        private_message(ANNA, contact=contact_of(ANNA, "+7 (999) 123-45-67"))
    )

    assert text_of(reply) == T.confirmed.format(name=", Anna")
    assert isinstance(reply.reply_markup, ReplyKeyboardRemove)
    polled = await harness.login.poll(nonce)
    assert polled.status is PollStatus.CONFIRMED
    assert polled.session is not None


@pytest.mark.parametrize("bare_digits", ["79991234567", "89991234567", "+79991234567"])
async def test_telegram_phone_formats_all_match(harness: Harness, bare_digits: str) -> None:
    nonce, _ = await open_link(harness)
    await harness.feed(private_message(ANNA, contact=contact_of(ANNA, bare_digits)))
    assert (await harness.login.poll(nonce)).status is PollStatus.CONFIRMED


async def test_foreign_contact_is_rejected(harness: Harness) -> None:
    """Eve opens her own link but shares Anna's contact card (Anna's phone and user id)."""
    nonce, _ = await open_link(harness, EVE)

    (reply,) = await harness.feed(private_message(EVE, contact=contact_of(ANNA, "89991234567")))

    assert text_of(reply) == T.foreign_contact
    assert reply.reply_markup is None  # the share button stays available for a retry
    polled = await harness.login.poll(nonce)
    assert polled.status is PollStatus.PENDING
    assert polled.session is None


async def test_contact_without_a_telegram_user_id_is_rejected(harness: Harness) -> None:
    nonce, _ = await open_link(harness, EVE)
    (reply,) = await harness.feed(private_message(EVE, contact=contact_of(None, "89991234567")))
    assert text_of(reply) == T.foreign_contact
    assert (await harness.login.poll(nonce)).status is PollStatus.PENDING


async def test_forwarded_contact_is_rejected_even_if_the_ids_match(harness: Harness) -> None:
    nonce, _ = await open_link(harness)
    update = private_message(
        ANNA, contact=contact_of(ANNA, "89991234567"), forwarded_from=user(ANNA)
    )
    (reply,) = await harness.feed(update)
    assert text_of(reply) == T.foreign_contact
    assert (await harness.login.poll(nonce)).status is PollStatus.PENDING


async def test_forwarded_contact_of_another_person_is_rejected(harness: Harness) -> None:
    nonce, _ = await open_link(harness, EVE)
    update = private_message(
        EVE, contact=contact_of(ANNA, "89991234567"), forwarded_from=user(ANNA)
    )
    (reply,) = await harness.feed(update)
    assert text_of(reply) == T.foreign_contact
    assert (await harness.login.poll(nonce)).status is PollStatus.PENDING


@pytest.mark.parametrize(
    ("phone", "expected"),
    [
        ("+7 999 555 00 00", T.not_found),
        ("89990000002", T.review_required),
        ("89990000003", T.review_required),
    ],
)
async def test_denied_logins_get_a_specific_message(
    harness: Harness, phone: str, expected: str
) -> None:
    nonce, _ = await open_link(harness)
    (reply,) = await harness.feed(private_message(ANNA, contact=contact_of(ANNA, phone)))
    assert text_of(reply) == expected
    assert (await harness.login.poll(nonce)).status is PollStatus.DENIED


async def test_contact_without_opening_a_link_first(harness: Harness) -> None:
    (reply,) = await harness.feed(private_message(ANNA, contact=contact_of(ANNA, "89991234567")))
    assert text_of(reply) == T.no_active_code


async def test_sharing_the_contact_twice(harness: Harness) -> None:
    await open_link(harness)
    update = private_message(ANNA, contact=contact_of(ANNA, "89991234567"))
    await harness.feed(update)
    (second,) = await harness.feed(update)
    assert text_of(second) == T.finished


async def test_customer_name_is_html_escaped(store: MemoryStore, clock: FakeClock) -> None:
    directory = InMemoryPhoneDirectory()
    directory.add("+79991234567", Customer("c-9", "<i>Mallory</i> & co"))
    harness = Harness(make_login(store, directory, clock))
    await open_link(harness)
    (reply,) = await harness.feed(private_message(ANNA, contact=contact_of(ANNA, "89991234567")))
    assert "&lt;i&gt;Mallory&lt;/i&gt; &amp; co" in text_of(reply)
    assert "<i>" not in text_of(reply)


async def test_texts_can_be_overridden(login: LoginService) -> None:
    harness = Harness(login, texts=Texts(welcome="Hello from {site}"))
    (reply,) = await harness.send_text(ANNA, "/start")
    assert text_of(reply) == "Hello from shop.example"


# -- chats and other messages ---------------------------------------------------------------


async def test_group_chats_are_ignored(harness: Harness) -> None:
    started = await harness.login.start()
    update = private_message(ANNA, text=f"/start {started.code}", chat_type="supergroup")
    assert await harness.feed(update) == []
    update = private_message(ANNA, contact=contact_of(ANNA, "89991234567"), chat_type="supergroup")
    assert await harness.feed(update) == []
    assert harness.session.requests == []


async def test_other_messages_get_a_hint(harness: Harness) -> None:
    (reply,) = await harness.send_text(ANNA, "hello")
    assert text_of(reply) == T.other


async def test_kill_switch_tells_the_user(
    store: MemoryStore, directory: InMemoryPhoneDirectory, clock: FakeClock
) -> None:
    harness = Harness(make_login(store, directory, clock, enabled=False))
    (reply,) = await harness.send_text(ANNA, f"/start {'C' * 32}")
    assert text_of(reply) == T.unavailable
    (reply,) = await harness.feed(private_message(ANNA, contact=contact_of(ANNA, "89991234567")))
    assert text_of(reply) == T.unavailable


# -- Bot API resilience ---------------------------------------------------------------------


def net() -> TelegramNetworkError:
    return TelegramNetworkError(method=GetMe(), message="handshake timed out")


async def test_replies_are_retried_on_network_errors(harness: Harness) -> None:
    harness.session.fail_next(net(), net())
    (reply,) = await harness.send_text(ANNA, "/start")
    assert text_of(reply) == T.welcome.format(site="shop.example")
    assert len(harness.session.requests) == 3
    assert len(harness.sleep.delays) == 2


async def test_a_dead_network_does_not_crash_the_handler(harness: Harness) -> None:
    harness.session.fail_next(*(net() for _ in range(3)))
    assert await harness.send_text(ANNA, "/start") == []  # nothing delivered, no exception
    assert len(harness.session.requests) == 3


async def test_login_state_survives_a_lost_reply(harness: Harness) -> None:
    """The user can still finish: the confirmation is stored before the reply is sent."""
    nonce, _ = await open_link(harness)
    harness.session.fail_next(*(net() for _ in range(3)))
    await harness.feed(private_message(ANNA, contact=contact_of(ANNA, "89991234567")))
    assert (await harness.login.poll(nonce)).status is PollStatus.CONFIRMED


def test_bot_type_is_importable() -> None:
    assert Bot is not None
