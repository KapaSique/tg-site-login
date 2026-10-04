"""Customer directory: the pluggable answer to "who owns this phone number?"."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable


class DenyReason(StrEnum):
    """Why a login was refused. The values are part of the public poll response."""

    NOT_FOUND = "not_found"
    REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True, slots=True)
class Customer:
    """The identity a confirmed login resolves to."""

    customer_id: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class LookupResult:
    """Either a customer or a deny reason, never both and never neither."""

    customer: Customer | None = None
    deny_reason: DenyReason | None = None

    def __post_init__(self) -> None:
        if (self.customer is None) == (self.deny_reason is None):
            msg = "exactly one of customer and deny_reason must be set"
            raise ValueError(msg)

    @classmethod
    def found(cls, customer: Customer) -> LookupResult:
        return cls(customer=customer)

    @classmethod
    def denied(cls, reason: DenyReason) -> LookupResult:
        return cls(deny_reason=reason)


@runtime_checkable
class PhoneDirectory(Protocol):
    """Resolve an E.164 phone number to a customer.

    Implementations must be safe to call concurrently. They should answer
    ``REVIEW_REQUIRED`` instead of guessing when the phone is ambiguous: logging a
    person in as the wrong customer is worse than asking them to contact support.
    """

    async def lookup(self, phone_e164: str) -> LookupResult: ...


@dataclass(frozen=True, slots=True)
class _Record:
    customer: Customer
    review_reasons: tuple[str, ...]


class InMemoryPhoneDirectory:
    """Directory kept in memory; used by tests and the demo deployment."""

    def __init__(self) -> None:
        self._by_phone: dict[str, list[_Record]] = defaultdict(list)

    def add(
        self,
        phone_e164: str,
        customer: Customer,
        *,
        review_reasons: Iterable[str] = (),
    ) -> None:
        """Register a customer. A non-empty ``review_reasons`` blocks automatic login."""
        self._by_phone[phone_e164].append(_Record(customer, tuple(review_reasons)))

    async def lookup(self, phone_e164: str) -> LookupResult:
        records = self._by_phone.get(phone_e164, [])
        if not records:
            return LookupResult.denied(DenyReason.NOT_FOUND)
        if len(records) > 1 or records[0].review_reasons:
            return LookupResult.denied(DenyReason.REVIEW_REQUIRED)
        return LookupResult.found(records[0].customer)

    @classmethod
    def from_json_file(cls, path: str | Path) -> InMemoryPhoneDirectory:
        """Load ``[{"phone": "+7...", "customer_id": "...", "name": "...",
        "review_reasons": []}, ...]``."""
        directory = cls()
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
        for row in rows:
            directory.add(
                row["phone"],
                Customer(row["customer_id"], row.get("name")),
                review_reasons=row.get("review_reasons", ()),
            )
        return directory
