"""Settings loaded from environment variables (and an optional ``.env`` file)."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_WEBHOOK_SECRET = re.compile(r"[A-Za-z0-9_-]{1,256}")
_MIN_API_TOKEN_LENGTH = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram
    bot_token: SecretStr
    bot_username: str  # without "@"; used to build the deep link
    site_name: str = "shop.example"  # shown to the user inside the bot
    telegram_connect_timeout: float = Field(default=5.0, gt=0)
    telegram_request_timeout: float = Field(default=15.0, gt=0)

    # Storage
    store_backend: Literal["redis", "memory"] = "redis"
    redis_url: str = "redis://localhost:6379/0"

    # Login
    login_enabled: bool = True  # kill switch: false stops start, poll and the bot flow
    login_code_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    client_ip_header: str | None = None  # e.g. X-Real-IP, only if your proxy overwrites it
    start_rate_limit: int = Field(default=10, ge=1)
    start_rate_window_seconds: int = Field(default=60, ge=1)
    poll_rate_limit: int = Field(default=120, ge=1)
    poll_rate_window_seconds: int = Field(default=60, ge=1)
    bad_code_limit: int = Field(default=5, ge=1)
    bad_code_window_seconds: int = Field(default=600, ge=1)
    cookie_secure: bool = True
    session_cookie_name: str = "session"
    session_ttl_seconds: int = Field(default=30 * 24 * 3600, ge=60)

    # Pluggable parts, as "package.module:callable"
    directory_factory: str | None = None  # called with Settings, returns a PhoneDirectory
    directory_file: str | None = None  # JSON file for the built-in in-memory directory
    session_issuer_factory: str | None = (
        None  # called with (Settings, Store), returns a SessionIssuer
    )

    # Notifications
    notify_enabled: bool = True  # kill switch for manager notifications
    notify_chat_id: int | str | None = None
    notify_api_token: SecretStr | None = None  # enables POST /internal/notify when set
    notify_max_attempts: int = Field(default=5, ge=1)
    notify_min_interval_seconds: float = Field(default=1.0, ge=0)

    # Bot runtime
    bot_mode: Literal["polling", "webhook"] = "polling"
    webhook_url: str | None = None
    webhook_secret: SecretStr | None = None
    webhook_host: str = "0.0.0.0"  # noqa: S104 - bound inside a container, exposed by the proxy
    webhook_port: int = 8081
    polling_timeout_seconds: int = Field(default=20, ge=1)

    log_level: str = "INFO"

    @field_validator(
        "client_ip_header",
        "directory_factory",
        "directory_file",
        "session_issuer_factory",
        "notify_api_token",
        "webhook_url",
        "webhook_secret",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """``NAME=`` in an .env file means "not set", not "set to the empty string"."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("notify_api_token")
    @classmethod
    def _long_enough(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < _MIN_API_TOKEN_LENGTH:
            msg = f"NOTIFY_API_TOKEN must be at least {_MIN_API_TOKEN_LENGTH} characters"
            raise ValueError(msg)
        return value

    @field_validator("bot_username")
    @classmethod
    def _strip_at(cls, value: str) -> str:
        return value.removeprefix("@").strip()

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip().rstrip("/") for item in value.split(",") if item.strip()]
        return value

    @field_validator("notify_chat_id", mode="before")
    @classmethod
    def _numeric_chat_id(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
        return value or None

    @model_validator(mode="after")
    def _check_webhook(self) -> Self:
        if self.bot_mode == "webhook":
            if not self.webhook_url or not self.webhook_url.startswith("https://"):
                msg = "WEBHOOK_URL must be an https:// URL when BOT_MODE=webhook"
                raise ValueError(msg)
            secret = self.webhook_secret.get_secret_value() if self.webhook_secret else ""
            if not _WEBHOOK_SECRET.fullmatch(secret):
                msg = "WEBHOOK_SECRET (1-256 chars of A-Z a-z 0-9 _ -) is required for webhooks"
                raise ValueError(msg)
        return self

    @property
    def nonce_cookie_name(self) -> str:
        """The ``__Host-`` prefix makes browsers refuse a cookie planted by a sibling subdomain."""
        return "__Host-tgsl_nonce" if self.cookie_secure else "tgsl_nonce"
