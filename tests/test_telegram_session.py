"""``ResilientAiohttpSession`` talks to a fake aiohttp session: no sockets are opened."""

from __future__ import annotations

import json
from types import TracebackType
from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import DeleteWebhook
from aiohttp import ClientConnectionError, ClientTimeout

from tg_site_login.config import Settings
from tg_site_login.telegram import ResilientAiohttpSession, build_bot

TOKEN = "123456789:TEST-TOKEN-NOT-REAL"


class FakeResponse:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self._body = json.dumps(body)

    async def text(self) -> str:
        return self._body


class FakePost:
    def __init__(self, outcome: FakeResponse | BaseException) -> None:
        self.outcome = outcome

    async def __aenter__(self) -> FakeResponse:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class FakeClientSession:
    def __init__(self, outcome: FakeResponse | BaseException) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakePost:
        self.calls.append({"url": url, **kwargs})
        return FakePost(self.outcome)


def session_with(
    outcome: FakeResponse | BaseException, monkeypatch: pytest.MonkeyPatch
) -> tuple[Bot, FakeClientSession]:
    session = ResilientAiohttpSession(connect_timeout=3.0, timeout=20.0)
    fake = FakeClientSession(outcome)

    async def create_session() -> Any:
        return fake

    monkeypatch.setattr(session, "create_session", create_session)
    return Bot(TOKEN, session=session), fake


async def test_connect_phase_has_its_own_short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    bot, fake = session_with(FakeResponse(200, {"ok": True, "result": True}), monkeypatch)
    assert await bot.session.make_request(bot, DeleteWebhook()) is True
    (call,) = fake.calls
    timeout = call["timeout"]
    assert isinstance(timeout, ClientTimeout)
    assert timeout.total == 20.0
    assert timeout.connect == 3.0
    assert timeout.sock_connect == 3.0


async def test_per_request_timeout_overrides_total_but_not_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, fake = session_with(FakeResponse(200, {"ok": True, "result": True}), monkeypatch)
    await bot.session.make_request(bot, DeleteWebhook(), timeout=70)
    timeout = fake.calls[0]["timeout"]
    assert (timeout.total, timeout.connect) == (70, 3.0)


async def test_timeouts_and_connection_errors_become_network_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for failure in (TimeoutError(), ClientConnectionError("reset by peer")):
        bot, _ = session_with(failure, monkeypatch)
        with pytest.raises(TelegramNetworkError):
            await bot.session.make_request(bot, DeleteWebhook())


async def test_api_errors_are_mapped_by_aiogram(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {
        "ok": False,
        "error_code": 429,
        "description": "Too Many Requests: retry after 4",
        "parameters": {"retry_after": 4},
    }
    bot, _ = session_with(FakeResponse(429, body), monkeypatch)
    with pytest.raises(TelegramRetryAfter) as caught:
        await bot.session.make_request(bot, DeleteWebhook())
    assert caught.value.retry_after == 4


def test_build_bot_uses_html_and_the_hardened_session(settings: Settings) -> None:
    bot = build_bot(settings)
    assert isinstance(bot.session, ResilientAiohttpSession)
    assert bot.session.connect_timeout == settings.telegram_connect_timeout
    assert bot.session.timeout == settings.telegram_request_timeout
    assert bot.default.parse_mode == "HTML"
