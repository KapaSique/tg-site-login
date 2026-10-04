"""Run the bot: long polling by default, webhook as an option."""

from __future__ import annotations

import logging

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.utils.backoff import BackoffConfig

from tg_site_login.bot.handlers import build_router
from tg_site_login.bot.webhook import create_webhook_app
from tg_site_login.config import Settings
from tg_site_login.login import LoginService
from tg_site_login.retry import RetryPolicy, call_with_retry
from tg_site_login.telegram import build_bot
from tg_site_login.wiring import build_login_service, build_store, configure_logging

logger = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message"]


def build_dispatcher(login: LoginService, settings: Settings) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(login, site_name=settings.site_name))
    return dispatcher


async def run_polling(bot: Bot, dispatcher: Dispatcher, settings: Settings) -> None:
    """Long polling. Needs no public URL and no inbound connections.

    getUpdates is retried by aiogram with its own backoff; the connect timeout of the
    session keeps a hung TCP handshake from stalling the loop for a minute.
    """
    policy = RetryPolicy(request_timeout=settings.telegram_request_timeout)
    # Polling and a webhook are mutually exclusive: make sure none is registered.
    await call_with_retry(
        lambda: bot.delete_webhook(drop_pending_updates=False), policy=policy, label="deleteWebhook"
    )
    await dispatcher.start_polling(
        bot,
        polling_timeout=settings.polling_timeout_seconds,
        backoff_config=BackoffConfig(min_delay=1.0, max_delay=15.0, factor=1.6, jitter=0.3),
        allowed_updates=ALLOWED_UPDATES,
    )


async def run_webhook(bot: Bot, dispatcher: Dispatcher, settings: Settings) -> None:
    """Register the webhook with a secret token and serve it with uvicorn."""
    if not settings.webhook_url or settings.webhook_secret is None:
        msg = "webhook mode needs WEBHOOK_URL and WEBHOOK_SECRET"
        raise RuntimeError(msg)
    url = settings.webhook_url
    secret = settings.webhook_secret.get_secret_value()
    policy = RetryPolicy(request_timeout=settings.telegram_request_timeout)
    await call_with_retry(
        lambda: bot.set_webhook(url, secret_token=secret, allowed_updates=ALLOWED_UPDATES),
        policy=policy,
        label="setWebhook",
    )
    app = create_webhook_app(bot, dispatcher, secret_token=secret)
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.webhook_host, port=settings.webhook_port, log_config=None)
    )
    await server.serve()


async def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    store = build_store(settings)
    bot = build_bot(settings)
    dispatcher = build_dispatcher(build_login_service(settings, store), settings)
    try:
        if settings.bot_mode == "webhook":
            await run_webhook(bot, dispatcher, settings)
        else:
            await run_polling(bot, dispatcher, settings)
    finally:
        await bot.session.close()
        await store.aclose()
