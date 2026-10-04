from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import FastAPI

from tests.support import FakeSession, make_bot, private_message
from tg_site_login.bot.handlers import build_router
from tg_site_login.bot.webhook import SECRET_HEADER, create_webhook_app
from tg_site_login.login import LoginService

SECRET = "s3cret_token-value"


@dataclass
class Rig:
    app: FastAPI
    session: FakeSession

    def client(self) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app)
        return httpx.AsyncClient(transport=transport, base_url="https://bot.example")


@pytest.fixture
def rig(login: LoginService) -> Rig:
    bot, session = make_bot()
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(login, site_name="shop.example"))
    return Rig(create_webhook_app(bot, dispatcher, secret_token=SECRET), session)


def start_update() -> dict[str, Any]:
    return private_message(111, text="/start").model_dump(mode="json", exclude_none=True)


async def test_valid_secret_feeds_the_dispatcher(rig: Rig) -> None:
    async with rig.client() as http:
        response = await http.post(
            "/telegram/webhook", json=start_update(), headers={SECRET_HEADER: SECRET}
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(rig.session.delivered) == 1
    assert "shop.example" in (rig.session.delivered[0].text or "")


@pytest.mark.parametrize(
    "headers",
    [{}, {SECRET_HEADER: ""}, {SECRET_HEADER: "wrong"}, {SECRET_HEADER: SECRET + "x"}],
)
async def test_wrong_or_missing_secret_is_rejected_before_parsing(
    rig: Rig, headers: dict[str, str]
) -> None:
    async with rig.client() as http:
        response = await http.post("/telegram/webhook", json=start_update(), headers=headers)
        garbage = await http.post("/telegram/webhook", content=b"not json", headers=headers)
    assert response.status_code == 403
    assert garbage.status_code == 403  # rejected on the secret, the body is never parsed
    assert rig.session.requests == []


async def test_invalid_update_is_a_400(rig: Rig) -> None:
    headers = {SECRET_HEADER: SECRET}
    async with rig.client() as http:
        not_json = await http.post("/telegram/webhook", content=b"nope", headers=headers)
        wrong_shape = await http.post("/telegram/webhook", json={"hello": 1}, headers=headers)
    assert not_json.status_code == 400
    assert wrong_shape.status_code == 400
    assert rig.session.requests == []


async def test_handler_failure_still_answers_200(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """A poisoned update must not be redelivered by Telegram forever."""

    async def explode(self: Dispatcher, bot: Bot, update: Update) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(Dispatcher, "feed_update", explode)
    async with rig.client() as http:
        response = await http.post(
            "/telegram/webhook", json=start_update(), headers={SECRET_HEADER: SECRET}
        )
    assert response.status_code == 200
    assert rig.session.requests == []


async def test_healthz(rig: Rig) -> None:
    async with rig.client() as http:
        assert (await http.get("/healthz")).status_code == 200
