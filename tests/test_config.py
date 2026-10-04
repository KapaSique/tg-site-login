from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from tg_site_login.config import Settings

BASE = {"BOT_TOKEN": "123456789:TEST-TOKEN-NOT-REAL", "BOT_USERNAME": "@example_shop_bot"}


def clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    known = {name.upper() for name in Settings.model_fields}
    for name in list(os.environ):
        if name.upper() in known:
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_env(monkeypatch)
    monkeypatch.chdir(tmp_path)  # no stray .env


def load(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    clear_env(monkeypatch)  # every call starts from a clean environment
    for key, value in (BASE | env).items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_defaults_are_the_safe_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load(monkeypatch)
    assert settings.bot_username == "example_shop_bot"  # a leading @ is tolerated
    assert settings.bot_mode == "polling"
    assert settings.cookie_secure is True
    assert settings.nonce_cookie_name == "__Host-tgsl_nonce"
    assert settings.login_enabled is True
    assert settings.notify_enabled is True
    assert settings.allowed_origins == []
    assert settings.login_code_ttl_seconds == 300
    assert settings.bot_token.get_secret_value() == BASE["BOT_TOKEN"]
    assert BASE["BOT_TOKEN"] not in repr(settings)  # the token never shows up in logs


def test_comma_separated_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load(monkeypatch, ALLOWED_ORIGINS="https://a.example, https://b.example/ ,")
    assert settings.allowed_origins == ["https://a.example", "https://b.example"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("-1001234567890", -1001234567890), ("42", 42), ("@managers", "@managers"), ("", None)],
)
def test_chat_id_parsing(monkeypatch: pytest.MonkeyPatch, raw: str, expected: object) -> None:
    assert load(monkeypatch, NOTIFY_CHAT_ID=raw).notify_chat_id == expected


def test_kill_switches_are_plain_booleans(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load(monkeypatch, LOGIN_ENABLED="false", NOTIFY_ENABLED="0")
    assert settings.login_enabled is False
    assert settings.notify_enabled is False


def test_insecure_cookie_changes_the_cookie_name(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load(monkeypatch, COOKIE_SECURE="false").nonce_cookie_name == "tgsl_nonce"


def test_webhook_mode_requires_https_url_and_a_valid_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="WEBHOOK_URL"):
        load(monkeypatch, BOT_MODE="webhook", WEBHOOK_SECRET="abc")
    with pytest.raises(ValidationError, match="WEBHOOK_URL"):
        load(
            monkeypatch,
            BOT_MODE="webhook",
            WEBHOOK_URL="http://bot.example/x",
            WEBHOOK_SECRET="abc",
        )
    with pytest.raises(ValidationError, match="WEBHOOK_SECRET"):
        load(monkeypatch, BOT_MODE="webhook", WEBHOOK_URL="https://bot.example/x")
    with pytest.raises(ValidationError, match="WEBHOOK_SECRET"):
        load(
            monkeypatch,
            BOT_MODE="webhook",
            WEBHOOK_URL="https://bot.example/x",
            WEBHOOK_SECRET="has spaces!",
        )
    ok = load(
        monkeypatch,
        BOT_MODE="webhook",
        WEBHOOK_URL="https://bot.example/telegram/webhook",
        WEBHOOK_SECRET="good_secret-1",
    )
    assert ok.bot_mode == "webhook"


def test_polling_mode_does_not_need_webhook_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load(monkeypatch, BOT_MODE="polling").webhook_url is None


def test_bot_token_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_USERNAME", "x_bot")
    with pytest.raises(ValidationError, match="bot_token"):
        Settings(_env_file=None)


def test_dotenv_file_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("BOT_TOKEN=1:abc\nBOT_USERNAME=file_bot\nSITE_NAME=file.example\n")
    settings = Settings(_env_file=env_file)
    assert (settings.bot_username, settings.site_name) == ("file_bot", "file.example")


def test_blank_values_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load(
        monkeypatch,
        NOTIFY_API_TOKEN="",
        CLIENT_IP_HEADER="  ",
        DIRECTORY_FACTORY="",
        WEBHOOK_SECRET="",
        NOTIFY_CHAT_ID="",
    )
    assert settings.notify_api_token is None  # an empty token must never enable the endpoint
    assert settings.client_ip_header is None
    assert settings.directory_factory is None
    assert settings.webhook_secret is None
    assert settings.notify_chat_id is None


def test_short_notify_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="at least 16"):
        load(monkeypatch, NOTIFY_API_TOKEN="too-short")
    assert load(monkeypatch, NOTIFY_API_TOKEN="x" * 16).notify_api_token is not None
