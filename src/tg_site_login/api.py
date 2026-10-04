"""FastAPI application: the browser-facing part of the login flow and a notify endpoint."""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from tg_site_login.config import Settings
from tg_site_login.events import (
    LeadCreated,
    NotificationEvent,
    OrderCreated,
    OrderItem,
    OrderUnpaid,
)
from tg_site_login.login import LoginDisabledError, LoginService, PollStatus
from tg_site_login.notifier import NotificationError, Notifier
from tg_site_login.ratelimit import RateLimiter
from tg_site_login.store import Store

logger = logging.getLogger(__name__)

_SAME_ORIGIN_FETCH_SITES = {"same-origin", "none"}


class StartResponse(BaseModel):
    deep_link: str
    expires_at: str
    pairing_code: str


class PollResponse(BaseModel):
    status: Literal["pending", "confirmed", "denied", "expired"]
    reason: Literal["not_found", "review_required"] | None = None


class OrderItemIn(BaseModel):
    title: str = Field(max_length=500)
    quantity: int = Field(ge=1, le=10_000)
    unit_price: Decimal = Field(ge=0)


class OrderCreatedIn(BaseModel):
    kind: Literal["order_created"]
    order_id: str = Field(min_length=1, max_length=64)
    customer_name: str = Field(max_length=200)
    items: list[OrderItemIn] = Field(max_length=500)
    currency: str = Field(default="RUB", max_length=8)
    phone: str | None = Field(default=None, max_length=32)
    payment_method: str | None = Field(default=None, max_length=100)
    comment: str | None = Field(default=None, max_length=5000)

    def to_event(self) -> NotificationEvent:
        return OrderCreated(
            order_id=self.order_id,
            customer_name=self.customer_name,
            items=tuple(OrderItem(i.title, i.quantity, i.unit_price) for i in self.items),
            currency=self.currency,
            phone=self.phone,
            payment_method=self.payment_method,
            comment=self.comment,
        )


class LeadCreatedIn(BaseModel):
    kind: Literal["lead_created"]
    lead_id: str = Field(min_length=1, max_length=64)
    name: str = Field(max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    topic: str | None = Field(default=None, max_length=200)
    message: str | None = Field(default=None, max_length=10_000)
    source: str | None = Field(default=None, max_length=200)

    def to_event(self) -> NotificationEvent:
        return LeadCreated(
            lead_id=self.lead_id,
            name=self.name,
            phone=self.phone,
            topic=self.topic,
            message=self.message,
            source=self.source,
        )


class OrderUnpaidIn(BaseModel):
    kind: Literal["order_unpaid"]
    order_id: str = Field(min_length=1, max_length=64)
    customer_name: str = Field(max_length=200)
    total: Decimal = Field(ge=0)
    waiting_minutes: int = Field(ge=0)
    currency: str = Field(default="RUB", max_length=8)
    phone: str | None = Field(default=None, max_length=32)
    payment_method: str | None = Field(default=None, max_length=100)

    def to_event(self) -> NotificationEvent:
        return OrderUnpaid(
            order_id=self.order_id,
            customer_name=self.customer_name,
            total=self.total,
            waiting_minutes=self.waiting_minutes,
            currency=self.currency,
            phone=self.phone,
            payment_method=self.payment_method,
        )


NotifyRequest = Annotated[
    OrderCreatedIn | LeadCreatedIn | OrderUnpaidIn, Field(discriminator="kind")
]


class NotifyResponse(BaseModel):
    result: str


def client_ip(request: Request, header: str | None) -> str:
    """Rate-limit key for the caller.

    ``header`` is trusted only when configured: your reverse proxy must overwrite it
    (not append to it), otherwise clients could pick their own limiter bucket.
    """
    if header:
        value = request.headers.get(header)
        if value:
            return value.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def origin_allowed(request: Request, allowed_origins: Sequence[str]) -> bool:
    """CSRF guard for state-changing browser requests.

    Browsers always send ``Origin`` on cross-origin POSTs; it must be the site itself or
    a configured origin. Without ``Origin`` (non-browser clients) browsers' Fetch Metadata
    header, when present, must say the request is same-origin.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return request.headers.get("sec-fetch-site", "same-origin") in _SAME_ORIGIN_FETCH_SITES
    if origin in allowed_origins:
        return True
    return urlsplit(origin).netloc == request.headers.get("host")


def _iso(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _set_cookie(
    response: Response, settings: Settings, name: str, value: str, max_age: int
) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def _register_notify_route(app: FastAPI, notifier: Notifier, token: str) -> None:
    expected = token.encode()

    async def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        valid = scheme.lower() == "bearer" and hmac.compare_digest(
            supplied.strip().encode(), expected
        )
        if not valid:
            raise HTTPException(
                status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"}
            )

    @app.post(
        "/internal/notify", response_model=NotifyResponse, dependencies=[Depends(require_token)]
    )
    async def notify(payload: NotifyRequest) -> NotifyResponse:
        try:
            outcome = await notifier.notify(payload.to_event())
        except NotificationError:
            logger.exception("notification failed")
            raise HTTPException(status_code=502, detail="delivery_failed") from None
        return NotifyResponse(result=outcome.value)


def create_app(
    *,
    settings: Settings,
    login: LoginService,
    store: Store,
    notifier: Notifier | None = None,
    on_shutdown: Sequence[Callable[[], Awaitable[None]]] = (),
) -> FastAPI:
    start_limiter = RateLimiter(
        store,
        "start",
        limit=settings.start_rate_limit,
        window=settings.start_rate_window_seconds,
    )
    poll_limiter = RateLimiter(
        store,
        "poll",
        limit=settings.poll_rate_limit,
        window=settings.poll_rate_window_seconds,
    )
    nonce_cookie = settings.nonce_cookie_name

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        for hook in on_shutdown:
            await hook()

    app = FastAPI(title="tg-site-login", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def no_store(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/auth/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def _login_disabled() -> HTTPException:
        return HTTPException(status_code=503, detail="login_disabled")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/auth/telegram/start", response_model=StartResponse)
    async def start(request: Request, response: Response) -> StartResponse:
        if not login.enabled:
            raise _login_disabled()
        if not origin_allowed(request, settings.allowed_origins):
            raise HTTPException(status_code=403, detail="origin_not_allowed")
        verdict = await start_limiter.hit(client_ip(request, settings.client_ip_header))
        if not verdict.allowed:
            raise HTTPException(
                status_code=429,
                detail="rate_limited",
                headers={"Retry-After": str(verdict.retry_after)},
            )
        try:
            started = await login.start(previous_nonce=request.cookies.get(nonce_cookie))
        except LoginDisabledError:
            raise _login_disabled() from None
        _set_cookie(
            response, settings, nonce_cookie, started.nonce, settings.login_code_ttl_seconds
        )
        return StartResponse(
            deep_link=f"https://t.me/{settings.bot_username}?start={started.code}",
            expires_at=_iso(started.expires_at),
            pairing_code=started.pairing_code,
        )

    @app.get("/auth/telegram/poll", response_model=PollResponse, response_model_exclude_none=True)
    async def poll(request: Request, response: Response) -> PollResponse:
        verdict = await poll_limiter.hit(client_ip(request, settings.client_ip_header))
        if not verdict.allowed:
            raise HTTPException(
                status_code=429,
                detail="rate_limited",
                headers={"Retry-After": str(verdict.retry_after)},
            )
        try:
            result = await login.poll(request.cookies.get(nonce_cookie))
        except LoginDisabledError:
            raise _login_disabled() from None
        if result.session is not None:
            session = result.session
            _set_cookie(response, settings, session.cookie_name, session.value, session.max_age)
        if result.status is not PollStatus.PENDING:
            # The attempt is over: drop the nonce so the browser stops sending it.
            response.delete_cookie(
                nonce_cookie,
                path="/",
                secure=settings.cookie_secure,
                httponly=True,
                samesite="lax",
            )
        reason = result.deny_reason.value if result.deny_reason else None
        return PollResponse.model_validate({"status": result.status.value, "reason": reason})

    if notifier is not None and settings.notify_api_token is not None:
        _register_notify_route(app, notifier, settings.notify_api_token.get_secret_value())

    return app
