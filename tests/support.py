"""Test doubles shared by the test modules. Nothing here touches the network."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, cast

import fakeredis
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import MessageOriginType, ParseMode
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import (
    Chat,
    Contact,
    Message,
    MessageOriginUser,
    Update,
    User,
)

from tg_site_login.directory import Customer, InMemoryPhoneDirectory

TOKEN = "123456789:TEST-TOKEN-NOT-REAL"
CHAT_ID = -1001234567890


class FakeClock:
    """Manual clock; usable both as a ``Store`` clock and as a wall clock."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleep:
    """Replaces ``asyncio.sleep`` and remembers the requested delays."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class FakeSession(BaseSession):
    """Bot API session that records requests and plays scripted failures."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.delivered: list[SendMessage] = []
        self.failures: deque[BaseException | str | None] = deque()
        self.yield_control = False  # let other tasks run inside a request
        self.streamed: list[bytes] = []  # what stream_content yields (file downloads)

    def fail_next(self, *errors: BaseException | str | None) -> None:
        """Queue one outcome per upcoming request.

        An exception is raised, the string ``"hang"`` never returns, ``None`` succeeds.
        """
        self.failures.extend(errors)

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109 - signature of the overridden method
    ) -> TelegramType:
        self.requests.append(method)
        if self.yield_control:
            await asyncio.sleep(0)
        if self.failures:
            failure = self.failures.popleft()
            if failure == "hang":
                await asyncio.Event().wait()
            elif isinstance(failure, BaseException):
                raise failure
        if isinstance(method, SendMessage):
            self.delivered.append(method)
            chat_id = method.chat_id if isinstance(method.chat_id, int) else 0
            result: Any = Message(
                message_id=len(self.delivered),
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type="private"),
                text=method.text,
            )
        else:
            result = True
        return cast(TelegramType, result)

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 - signature of the overridden method
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        for chunk in self.streamed:
            yield chunk


def make_bot(session: FakeSession | None = None) -> tuple[Bot, FakeSession]:
    fake = session or FakeSession()
    bot = Bot(TOKEN, session=fake, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    return bot, fake


def user(user_id: int, first_name: str = "Test") -> User:
    return User(id=user_id, is_bot=False, first_name=first_name)


def private_message(
    user_id: int,
    *,
    text: str | None = None,
    contact: Contact | None = None,
    forwarded_from: User | None = None,
    chat_type: str = "private",
    update_id: int = 1,
) -> Update:
    sender = user(user_id)
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=user_id if chat_type == "private" else -100500, type=chat_type),
        from_user=sender,
        text=text,
        contact=contact,
        forward_origin=(
            MessageOriginUser(
                type=MessageOriginType.USER, date=datetime.now(UTC), sender_user=forwarded_from
            )
            if forwarded_from
            else None
        ),
    )
    return Update(update_id=update_id, message=message)


def contact_of(user_id: int | None, phone: str, name: str = "Test") -> Contact:
    return Contact(phone_number=phone, first_name=name, user_id=user_id)


def make_directory() -> InMemoryPhoneDirectory:
    """Fictional customers; all numbers are in the 999 range used for examples."""
    directory = InMemoryPhoneDirectory()
    directory.add("+79991234567", Customer("c-1", "Anna"))
    directory.add("+79990000002", Customer("c-2", "Boris"), review_reasons=["name_mismatch"])
    directory.add("+79990000003", Customer("c-3", "Clara"))
    directory.add("+79990000003", Customer("c-4", "Clara"))  # same phone twice: ambiguous
    return directory


def new_redis() -> fakeredis.FakeAsyncRedis:
    """A Redis double with its own private server, so tests never share state."""
    return fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)
