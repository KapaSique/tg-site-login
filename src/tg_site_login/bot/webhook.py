"""Webhook receiver for the bot, protected by Telegram's secret-token header."""

from __future__ import annotations

import hmac
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105 - header name, not a secret


def create_webhook_app(bot: Bot, dispatcher: Dispatcher, *, secret_token: str) -> FastAPI:
    """``POST /telegram/webhook``: verify the secret, parse the update, feed the dispatcher.

    The secret is the value passed to ``setWebhook(secret_token=...)``; Telegram echoes it in
    the ``X-Telegram-Bot-Api-Secret-Token`` header of every delivery. Anything without the
    right header is rejected before the body is parsed.
    """
    app = FastAPI(title="tg-site-login webhook", docs_url=None, redoc_url=None)
    expected = secret_token.encode()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/telegram/webhook")
    async def webhook(request: Request) -> dict[str, bool]:
        supplied = request.headers.get(SECRET_HEADER, "").encode()
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="bad_secret")
        try:
            update = Update.model_validate(await request.json(), context={"bot": bot})
        except (ValueError, ValidationError):
            raise HTTPException(status_code=400, detail="bad_update") from None
        try:
            await dispatcher.feed_update(bot, update)
        except Exception:
            # Answer 200 anyway: Telegram would redeliver a poisoned update forever.
            logger.exception("update %s failed", update.update_id)
        return {"ok": True}

    return app
