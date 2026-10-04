"""The login state machine: one-time codes bound to a browser nonce.

Data model (every key lives in the ``Store`` and expires with the code):

``login:rec:<H(code)>``    immutable record: hash of the browser nonce, pairing code, expiry
``login:nonce:<H(nonce)>`` index from the browser nonce to the code, so the poll needs no argument
``login:claim:<H(code)>``  Telegram user id that opened the deep link (set once)
``login:res:<H(code)>``    outcome written by the bot (set once): confirmed or denied
``login:user:<tg id>``     the code a Telegram user is currently confirming (expires on its own)

Codes and nonces are never stored in plain text. The outcome is written once and the
record is burned with an atomic get-and-delete, so a code yields at most one session.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from tg_site_login.directory import Customer, DenyReason, PhoneDirectory
from tg_site_login.phone import mask_phone, normalize_phone
from tg_site_login.ratelimit import RateLimiter
from tg_site_login.sessions import IssuedSession, SessionIssuer
from tg_site_login.store import Store

logger = logging.getLogger(__name__)

# Telegram allows A-Z a-z 0-9 _ - and up to 64 characters in a deep-link payload.
_CODE_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,64}")
_MAX_NONCE_LENGTH = 128


class LoginDisabledError(RuntimeError):
    """Raised when the login kill switch is on."""


class PollStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DENIED = "denied"
    EXPIRED = "expired"


class ClaimStatus(StrEnum):
    OK = "ok"
    INVALID = "invalid"  # unknown, malformed or expired code
    TAKEN = "taken"  # another Telegram user already opened this code
    FINISHED = "finished"  # the code already has an outcome
    RATE_LIMITED = "rate_limited"  # too many bad codes from this Telegram user


class SubmitStatus(StrEnum):
    CONFIRMED = "confirmed"
    DENIED = "denied"
    FOREIGN_CONTACT = "foreign_contact"  # contact does not belong to the sender
    NO_ACTIVE_CODE = "no_active_code"
    ALREADY_FINISHED = "already_finished"


@dataclass(frozen=True, slots=True)
class StartedLogin:
    code: str
    nonce: str
    pairing_code: str
    expires_at: float  # unix time


@dataclass(frozen=True, slots=True)
class ClaimResult:
    status: ClaimStatus
    pairing_code: str | None = None


@dataclass(frozen=True, slots=True)
class SubmitResult:
    status: SubmitStatus
    deny_reason: DenyReason | None = None
    customer: Customer | None = None


@dataclass(frozen=True, slots=True)
class PollResult:
    status: PollStatus
    deny_reason: DenyReason | None = None
    session: IssuedSession | None = None


@dataclass(frozen=True, slots=True)
class _Record:
    nonce_hash: str
    pairing_code: str
    expires_at: float


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class LoginService:
    """Shared by the API process (start, poll) and the bot process (claim, submit)."""

    def __init__(
        self,
        store: Store,
        directory: PhoneDirectory,
        issuer: SessionIssuer,
        *,
        ttl: float = 300.0,
        clock: Callable[[], float] = time.time,
        bad_code_limiter: RateLimiter | None = None,
        enabled: bool = True,
    ) -> None:
        self._store = store
        self._directory = directory
        self._issuer = issuer
        self._ttl = ttl
        self._clock = clock
        self._bad_code_limiter = bad_code_limiter
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise LoginDisabledError

    # -- API side -------------------------------------------------------------------------

    async def start(self, *, previous_nonce: str | None = None) -> StartedLogin:
        """Create a login attempt; a previous attempt of the same browser is revoked."""
        self._require_enabled()
        if previous_nonce:
            await self._revoke(previous_nonce)
        code = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(32)
        pairing_code = f"{secrets.randbelow(10_000):04d}"
        expires_at = self._clock() + self._ttl
        code_hash, nonce_hash = _digest(code), _digest(nonce)
        record = json.dumps({"nh": nonce_hash, "pairing": pairing_code, "exp": expires_at})
        await self._store.set(f"login:rec:{code_hash}", record, self._ttl)
        await self._store.set(f"login:nonce:{nonce_hash}", code_hash, self._ttl)
        return StartedLogin(code, nonce, pairing_code, expires_at)

    async def poll(self, nonce: str | None) -> PollResult:
        """Report the attempt bound to ``nonce``; a confirmed attempt is burned here."""
        self._require_enabled()
        if not nonce or len(nonce) > _MAX_NONCE_LENGTH:
            return PollResult(PollStatus.EXPIRED)
        nonce_hash = _digest(nonce)
        code_hash = await self._store.get(f"login:nonce:{nonce_hash}")
        if code_hash is None:
            return PollResult(PollStatus.EXPIRED)
        record = await self._load(code_hash)
        if record is None or not hmac.compare_digest(record.nonce_hash, nonce_hash):
            return PollResult(PollStatus.EXPIRED)

        raw = await self._store.get(f"login:res:{code_hash}")
        if raw is None:
            return PollResult(PollStatus.PENDING)
        outcome = json.loads(raw)
        if outcome["status"] == "denied":
            return PollResult(PollStatus.DENIED, deny_reason=DenyReason(outcome["reason"]))

        # Confirmed: exactly one concurrent poll wins the atomic get-and-delete.
        if await self._store.get_and_delete(f"login:rec:{code_hash}") is None:
            return PollResult(PollStatus.EXPIRED)
        await self._cleanup(code_hash, nonce_hash)
        customer = Customer(outcome["customer_id"], outcome["name"])
        session = await self._issuer.issue(customer)
        logger.info("login confirmed", extra={"code": code_hash[:8]})
        return PollResult(PollStatus.CONFIRMED, session=session)

    # -- Bot side -------------------------------------------------------------------------

    async def claim(self, code: str, tg_user_id: int) -> ClaimResult:
        """A Telegram user opened the deep link; bind the code to them."""
        self._require_enabled()
        code_hash = _digest(code)
        record = await self._load(code_hash) if _CODE_PATTERN.fullmatch(code) else None
        if record is None:
            return await self._bad_code(tg_user_id)
        if await self._store.get(f"login:res:{code_hash}") is not None:
            return ClaimResult(ClaimStatus.FINISHED)
        remaining = self._remaining(record)
        claimed = await self._store.set(
            f"login:claim:{code_hash}", str(tg_user_id), remaining, nx=True
        )
        if not claimed and await self._store.get(f"login:claim:{code_hash}") != str(tg_user_id):
            return ClaimResult(ClaimStatus.TAKEN)
        await self._store.set(f"login:user:{tg_user_id}", code_hash, remaining)
        return ClaimResult(ClaimStatus.OK, pairing_code=record.pairing_code)

    async def submit_contact(
        self, tg_user_id: int, contact_user_id: int | None, phone_raw: str
    ) -> SubmitResult:
        """The user shared a contact. Only their own contact is accepted."""
        self._require_enabled()
        if contact_user_id != tg_user_id:
            logger.warning("foreign contact rejected", extra={"tg_user": tg_user_id})
            return SubmitResult(SubmitStatus.FOREIGN_CONTACT)

        code_hash = await self._store.get(f"login:user:{tg_user_id}")
        record = await self._load(code_hash) if code_hash else None
        if code_hash is None or record is None:
            return SubmitResult(SubmitStatus.NO_ACTIVE_CODE)
        if await self._store.get(f"login:claim:{code_hash}") != str(tg_user_id):
            return SubmitResult(SubmitStatus.NO_ACTIVE_CODE)
        if await self._store.get(f"login:res:{code_hash}") is not None:
            return SubmitResult(SubmitStatus.ALREADY_FINISHED)

        phone = normalize_phone(phone_raw)
        customer: Customer | None = None
        reason = DenyReason.NOT_FOUND
        if phone is not None:
            found = await self._directory.lookup(phone)
            customer = found.customer
            if found.deny_reason is not None:
                reason = found.deny_reason
            logger.info(
                "directory lookup",
                extra={"phone": mask_phone(phone), "found": customer is not None},
            )

        if customer is not None:
            payload = {
                "status": "confirmed",
                "customer_id": customer.customer_id,
                "name": customer.display_name,
            }
        else:
            payload = {"status": "denied", "reason": reason.value}
        written = await self._store.set(
            f"login:res:{code_hash}", json.dumps(payload), self._remaining(record), nx=True
        )
        if not written:
            return SubmitResult(SubmitStatus.ALREADY_FINISHED)
        if customer is not None:
            return SubmitResult(SubmitStatus.CONFIRMED, customer=customer)
        return SubmitResult(SubmitStatus.DENIED, deny_reason=reason)

    # -- internals ------------------------------------------------------------------------

    async def _load(self, code_hash: str) -> _Record | None:
        raw = await self._store.get(f"login:rec:{code_hash}")
        if raw is None:
            return None
        data = json.loads(raw)
        record = _Record(data["nh"], data["pairing"], float(data["exp"]))
        if self._clock() >= record.expires_at:
            return None
        return record

    def _remaining(self, record: _Record) -> float:
        return max(1.0, record.expires_at - self._clock())

    async def _bad_code(self, tg_user_id: int) -> ClaimResult:
        if self._bad_code_limiter is not None:
            verdict = await self._bad_code_limiter.hit(str(tg_user_id))
            if not verdict.allowed:
                return ClaimResult(ClaimStatus.RATE_LIMITED)
        return ClaimResult(ClaimStatus.INVALID)

    async def _revoke(self, nonce: str) -> None:
        if len(nonce) > _MAX_NONCE_LENGTH:
            return
        nonce_hash = _digest(nonce)
        code_hash = await self._store.get(f"login:nonce:{nonce_hash}")
        if code_hash is not None:
            await self._store.delete(f"login:rec:{code_hash}")
            await self._cleanup(code_hash, nonce_hash)

    async def _cleanup(self, code_hash: str, nonce_hash: str) -> None:
        await self._store.delete(f"login:nonce:{nonce_hash}")
        await self._store.delete(f"login:claim:{code_hash}")
        await self._store.delete(f"login:res:{code_hash}")
