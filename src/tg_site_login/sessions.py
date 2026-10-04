"""Pluggable session issuing: what happens once a login is confirmed."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tg_site_login.directory import Customer
from tg_site_login.store import Store


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A session cookie the API sets on the browser that completed the login."""

    cookie_name: str
    value: str
    max_age: int  # seconds


@runtime_checkable
class SessionIssuer(Protocol):
    """Create a session for a confirmed customer.

    This is the integration point with the host application. It is called at most once
    per login attempt, after the login code has been burned.
    """

    async def issue(self, customer: Customer) -> IssuedSession: ...


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class StoreSessionIssuer:
    """Reference issuer: random opaque token, only its hash is kept in the ``Store``."""

    def __init__(
        self,
        store: Store,
        *,
        cookie_name: str = "session",
        ttl_seconds: int = 30 * 24 * 3600,
    ) -> None:
        self._store = store
        self._cookie_name = cookie_name
        self._ttl = ttl_seconds

    async def issue(self, customer: Customer) -> IssuedSession:
        token = secrets.token_urlsafe(32)
        payload = json.dumps({"id": customer.customer_id, "name": customer.display_name})
        await self._store.set(f"session:{_digest(token)}", payload, self._ttl)
        return IssuedSession(self._cookie_name, token, self._ttl)

    async def resolve(self, token: str) -> Customer | None:
        """Return the customer behind a session token, if it is still valid."""
        raw = await self._store.get(f"session:{_digest(token)}")
        if raw is None:
            return None
        data = json.loads(raw)
        return Customer(data["id"], data["name"])
