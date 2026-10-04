"""Build the object graph from ``Settings``; shared by the API and the bot processes."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from typing import Any, cast

from fastapi import FastAPI

from tg_site_login.api import create_app
from tg_site_login.config import Settings
from tg_site_login.directory import InMemoryPhoneDirectory, PhoneDirectory
from tg_site_login.login import LoginService
from tg_site_login.notifier import Notifier
from tg_site_login.ratelimit import RateLimiter
from tg_site_login.redis_store import RedisStore
from tg_site_login.retry import RetryPolicy
from tg_site_login.sessions import SessionIssuer, StoreSessionIssuer
from tg_site_login.store import MemoryStore, Store
from tg_site_login.telegram import build_bot


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Request URLs of the Bot API contain the bot token; never log them.
    for noisy in ("httpx", "httpcore", "aiohttp.client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def load_object(spec: str) -> Any:
    """Import ``package.module:attribute``."""
    module_name, _, attribute = spec.partition(":")
    if not module_name or not attribute:
        msg = f"expected 'package.module:attribute', got {spec!r}"
        raise ValueError(msg)
    return getattr(importlib.import_module(module_name), attribute)


def build_store(settings: Settings) -> Store:
    if settings.store_backend == "memory":
        return MemoryStore()
    return RedisStore.from_url(settings.redis_url)


def build_directory(settings: Settings) -> PhoneDirectory:
    if settings.directory_factory:
        factory = cast(
            Callable[[Settings], PhoneDirectory], load_object(settings.directory_factory)
        )
        return factory(settings)
    if settings.directory_file:
        return InMemoryPhoneDirectory.from_json_file(settings.directory_file)
    return InMemoryPhoneDirectory()


def build_issuer(settings: Settings, store: Store) -> SessionIssuer:
    if settings.session_issuer_factory:
        factory = cast(
            Callable[[Settings, Store], SessionIssuer], load_object(settings.session_issuer_factory)
        )
        return factory(settings, store)
    return StoreSessionIssuer(
        store,
        cookie_name=settings.session_cookie_name,
        ttl_seconds=settings.session_ttl_seconds,
    )


def build_login_service(settings: Settings, store: Store) -> LoginService:
    return LoginService(
        store,
        build_directory(settings),
        build_issuer(settings, store),
        ttl=settings.login_code_ttl_seconds,
        bad_code_limiter=RateLimiter(
            store,
            "badcode",
            limit=settings.bad_code_limit,
            window=settings.bad_code_window_seconds,
        ),
        enabled=settings.login_enabled,
    )


def build_retry_policy(settings: Settings) -> RetryPolicy:
    return RetryPolicy(
        max_attempts=settings.notify_max_attempts,
        request_timeout=settings.telegram_request_timeout,
    )


def create_app_from_env() -> FastAPI:
    """Application factory for ``uvicorn tg_site_login.wiring:create_app_from_env --factory``."""
    settings = Settings()
    configure_logging(settings.log_level)
    store = build_store(settings)
    login = build_login_service(settings, store)
    notifier: Notifier | None = None
    shutdown = [store.aclose]
    if settings.notify_chat_id is not None and settings.notify_api_token is not None:
        bot = build_bot(settings)
        notifier = Notifier(
            bot,
            settings.notify_chat_id,
            store,
            policy=build_retry_policy(settings),
            is_enabled=lambda: settings.notify_enabled,
            min_interval=settings.notify_min_interval_seconds,
        )
        shutdown.append(bot.session.close)
    return create_app(
        settings=settings, login=login, store=store, notifier=notifier, on_shutdown=shutdown
    )
