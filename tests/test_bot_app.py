"""The runners: polling and webhook wiring, checked without opening a network connection."""

from __future__ import annotations

from typing import Any

import pytest
import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.methods import DeleteWebhook, SetWebhook
from pydantic import SecretStr

from tests.conftest import make_login
from tests.support import make_bot
from tg_site_login.bot import app as bot_app
from tg_site_login.config import Settings
from tg_site_login.login import LoginService


def webhook_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "bot_mode": "webhook",
            "webhook_url": "https://bot.example/telegram/webhook",
            "webhook_secret": SecretStr("good_secret-1"),
        }
    )


async def test_polling_clears_any_webhook_then_polls(
    login: LoginService, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, session = make_bot()
    dispatcher = bot_app.build_dispatcher(login, settings)
    captured: dict[str, Any] = {}

    async def fake_start_polling(self: Dispatcher, *bots: Bot, **kwargs: Any) -> None:
        captured["bots"] = bots
        captured["kwargs"] = kwargs
        # polling must start only after the webhook was removed
        assert [type(r) for r in session.requests] == [DeleteWebhook]

    monkeypatch.setattr(Dispatcher, "start_polling", fake_start_polling)

    await bot_app.run_polling(bot, dispatcher, settings)

    (delete,) = session.requests
    assert isinstance(delete, DeleteWebhook)
    assert delete.drop_pending_updates is False  # do not lose updates queued while down
    assert captured["bots"] == (bot,)
    assert captured["kwargs"]["allowed_updates"] == ["message"]
    assert captured["kwargs"]["polling_timeout"] == settings.polling_timeout_seconds
    assert captured["kwargs"]["backoff_config"].max_delay == 15.0


async def test_webhook_mode_registers_with_a_secret_and_serves(
    login: LoginService, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, session = make_bot()
    configured = webhook_settings(settings)
    dispatcher = bot_app.build_dispatcher(login, configured)
    served: dict[str, Any] = {}

    async def fake_serve(self: uvicorn.Server, sockets: Any = None) -> None:
        served["host"] = self.config.host
        served["port"] = self.config.port
        served["app"] = self.config.app

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)

    await bot_app.run_webhook(bot, dispatcher, configured)

    (request,) = session.requests
    assert isinstance(request, SetWebhook)
    assert request.url == "https://bot.example/telegram/webhook"
    assert request.secret_token == "good_secret-1"
    assert request.allowed_updates == ["message"]
    assert served["port"] == configured.webhook_port
    assert served["app"].title == "tg-site-login webhook"


async def test_main_picks_the_runner_from_the_mode(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    bot, _ = make_bot()

    async def fake_polling(*args: Any) -> None:
        calls.append("polling")

    async def fake_webhook(*args: Any) -> None:
        calls.append("webhook")

    monkeypatch.setattr(bot_app, "build_bot", lambda _settings: bot)
    monkeypatch.setattr(bot_app, "run_polling", fake_polling)
    monkeypatch.setattr(bot_app, "run_webhook", fake_webhook)
    monkeypatch.setattr(bot_app, "configure_logging", lambda _level: None)
    monkeypatch.setattr(
        bot_app, "build_login_service", lambda s, store: make_login(store, _empty(), _clock())
    )

    for mode, expected in (("polling", "polling"), ("webhook", "webhook")):
        monkeypatch.setenv("BOT_TOKEN", "123456789:TEST-TOKEN-NOT-REAL")
        monkeypatch.setenv("BOT_USERNAME", "example_shop_bot")
        monkeypatch.setenv("STORE_BACKEND", "memory")
        monkeypatch.setenv("BOT_MODE", mode)
        monkeypatch.setenv("WEBHOOK_URL", "https://bot.example/hook")
        monkeypatch.setenv("WEBHOOK_SECRET", "abc")
        await bot_app.main()
        assert calls[-1] == expected


def _empty() -> Any:
    from tg_site_login.directory import InMemoryPhoneDirectory

    return InMemoryPhoneDirectory()


def _clock() -> Any:
    from tests.support import FakeClock

    return FakeClock()
