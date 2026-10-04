"""aiogram router implementing the bot side of the login flow."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import AiogramError
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from tg_site_login.bot.texts import Texts
from tg_site_login.directory import DenyReason
from tg_site_login.htmlfmt import escape
from tg_site_login.login import ClaimStatus, LoginDisabledError, LoginService, SubmitStatus
from tg_site_login.retry import RetryPolicy, Sleep, call_with_retry

logger = logging.getLogger(__name__)

Markup = ReplyKeyboardMarkup | ReplyKeyboardRemove


class LoginHandlers:
    """Handlers for ``/start <code>``, the shared contact and everything else."""

    def __init__(
        self,
        login: LoginService,
        *,
        site_name: str,
        texts: Texts | None = None,
        policy: RetryPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._login = login
        self._site = escape(site_name)
        self._t = texts or Texts()
        self._policy = policy or RetryPolicy()
        self._sleep = sleep
        self._rng = rng
        self._claim_texts = {
            ClaimStatus.INVALID: self._t.invalid_code,
            ClaimStatus.TAKEN: self._t.taken,
            ClaimStatus.FINISHED: self._t.finished,
            ClaimStatus.RATE_LIMITED: self._t.rate_limited,
        }
        self._deny_texts = {
            DenyReason.NOT_FOUND: self._t.not_found,
            DenyReason.REVIEW_REQUIRED: self._t.review_required,
        }

    def build_router(self) -> Router:
        router = Router(name="tg_site_login")
        private = F.chat.type == ChatType.PRIVATE
        # Order matters: the first matching handler wins.
        router.message.register(self.on_start_with_code, CommandStart(deep_link=True), private)
        router.message.register(self.on_start, CommandStart(), private)
        router.message.register(self.on_contact, F.contact, private)
        router.message.register(self.on_other, private)
        return router

    async def _reply(self, message: Message, text: str, markup: Markup | None = None) -> None:
        def send() -> Awaitable[Message]:
            return message.answer(text, reply_markup=markup)

        try:
            await call_with_retry(
                send, policy=self._policy, sleep=self._sleep, rng=self._rng, label="bot reply"
            )
        except (AiogramError, TimeoutError):
            # The login state is already stored; the user can simply press the button again.
            logger.exception("could not deliver a bot reply")

    async def on_start_with_code(self, message: Message, command: CommandObject) -> None:
        if message.from_user is None:
            return
        try:
            claim = await self._login.claim(command.args or "", message.from_user.id)
        except LoginDisabledError:
            await self._reply(message, self._t.unavailable, ReplyKeyboardRemove())
            return
        if claim.status is not ClaimStatus.OK:
            await self._reply(message, self._claim_texts[claim.status], ReplyKeyboardRemove())
            return
        keyboard = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=self._t.share_button, request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        text = self._t.ask_contact.format(site=self._site, pairing=escape(claim.pairing_code or ""))
        await self._reply(message, text, keyboard)

    async def on_start(self, message: Message) -> None:
        await self._reply(message, self._t.welcome.format(site=self._site))

    async def on_contact(self, message: Message) -> None:
        contact = message.contact
        if contact is None or message.from_user is None:
            return
        # A contact picked from the address book or forwarded from a chat is not a proof of
        # ownership, so only the non-forwarded request_contact button result is accepted.
        if message.forward_origin is not None:
            await self._reply(message, self._t.foreign_contact, ReplyKeyboardRemove())
            return
        try:
            result = await self._login.submit_contact(
                tg_user_id=message.from_user.id,
                contact_user_id=contact.user_id,
                phone_raw=contact.phone_number,
            )
        except LoginDisabledError:
            await self._reply(message, self._t.unavailable, ReplyKeyboardRemove())
            return

        remove = ReplyKeyboardRemove()
        match result.status:
            case SubmitStatus.CONFIRMED:
                customer_name = result.customer.display_name if result.customer else None
                name = f", {escape(customer_name)}" if customer_name else ""
                await self._reply(message, self._t.confirmed.format(name=name), remove)
            case SubmitStatus.DENIED:
                reason = result.deny_reason or DenyReason.NOT_FOUND
                await self._reply(message, self._deny_texts[reason], remove)
            case SubmitStatus.FOREIGN_CONTACT:
                # Keep the share button on screen so the user can retry with their own number.
                await self._reply(message, self._t.foreign_contact)
            case SubmitStatus.ALREADY_FINISHED:
                await self._reply(message, self._t.finished, remove)
            case SubmitStatus.NO_ACTIVE_CODE:
                await self._reply(message, self._t.no_active_code, remove)

    async def on_other(self, message: Message) -> None:
        await self._reply(message, self._t.other)


def build_router(
    login: LoginService,
    *,
    site_name: str,
    texts: Texts | None = None,
    policy: RetryPolicy | None = None,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
) -> Router:
    """Create the login router. All Bot API replies go through timeouts and retries."""
    handlers = LoginHandlers(
        login, site_name=site_name, texts=texts, policy=policy, sleep=sleep, rng=rng
    )
    return handlers.build_router()
