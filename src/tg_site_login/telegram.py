"""Bot construction with a session that cuts hung TCP handshakes quickly."""

from __future__ import annotations

from typing import cast

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.base import DEFAULT_TIMEOUT
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiohttp import ClientError, ClientTimeout

from tg_site_login.config import Settings


class ResilientAiohttpSession(AiohttpSession):
    """``AiohttpSession`` with a separate, short connect timeout.

    The stock session applies a single total timeout to a request, so a TCP handshake
    that never completes blocks for the whole timeout. Here the connect phase gets its
    own limit and fails fast with ``TelegramNetworkError``, which the retry layer and
    aiogram's polling backoff then handle.
    """

    def __init__(self, *, connect_timeout: float = 5.0, timeout: float = DEFAULT_TIMEOUT) -> None:
        super().__init__(timeout=timeout)
        self.connect_timeout = connect_timeout

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,  # noqa: ASYNC109 - signature of the overridden method
    ) -> TelegramType:
        session = await self.create_session()
        url = self.api.api_url(token=bot.token, method=method.__api_method__)
        form = self.build_form_data(bot=bot, method=method)
        client_timeout = ClientTimeout(
            total=self.timeout if timeout is None else timeout,
            connect=self.connect_timeout,
            sock_connect=self.connect_timeout,
        )
        try:
            async with session.post(url, data=form, timeout=client_timeout) as resp:
                raw_result = await resp.text()
        except TimeoutError as exc:
            raise TelegramNetworkError(method=method, message="Request timeout error") from exc
        except ClientError as exc:
            message = f"{type(exc).__name__}: {exc}"
            raise TelegramNetworkError(method=method, message=message) from exc
        response = self.check_response(
            bot=bot, method=method, status_code=resp.status, content=raw_result
        )
        return cast(TelegramType, response.result)


def build_bot(settings: Settings) -> Bot:
    """Create a ``Bot`` with HTML parse mode and hardened timeouts."""
    session = ResilientAiohttpSession(
        connect_timeout=settings.telegram_connect_timeout,
        timeout=settings.telegram_request_timeout,
    )
    return Bot(
        token=settings.bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
