"""Example SQL-backed ``PhoneDirectory``.

The adapter does not depend on a database driver. You provide a ``RowFetcher`` that runs
a parameterized query, for example a thin wrapper around ``asyncpg``::

    class AsyncpgFetcher:
        def __init__(self, pool: asyncpg.Pool) -> None:
            self._pool = pool

        async def fetch_all(
            self, query: str, params: Sequence[object]
        ) -> Sequence[Mapping[str, Any]]:
            return await self._pool.fetch(query, *params)

Expected table (names are configurable through ``query``)::

    CREATE TABLE customer_directory (
        customer_id    text PRIMARY KEY,
        display_name   text,
        phone_e164     text NOT NULL,
        review_reasons jsonb NOT NULL DEFAULT '[]',
        source_count   integer NOT NULL DEFAULT 0
    );
    CREATE INDEX ON customer_directory (phone_e164);

Rules applied to the rows returned for one phone:

* no rows                                  -> ``not_found``
* more than one row                        -> ``review_required`` (ambiguous phone)
* one row with ``source_count == 0``       -> ``not_found`` (placeholder, no real source)
* one row with non-empty ``review_reasons``-> ``review_required``
* otherwise                                -> the customer
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from tg_site_login.directory import Customer, DenyReason, LookupResult

DEFAULT_QUERY = """
SELECT customer_id, display_name, review_reasons, source_count
FROM customer_directory
WHERE phone_e164 = $1
LIMIT 2
"""


class RowFetcher(Protocol):
    """Runs a query with positional parameters and returns rows as mappings."""

    async def fetch_all(
        self, query: str, params: Sequence[object]
    ) -> Sequence[Mapping[str, Any]]: ...


def _has_review_reasons(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return False
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            return True  # an unparseable value is treated as "needs a human"
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return bool(value)


class SqlPhoneDirectory:
    """``PhoneDirectory`` over any SQL database reachable through a ``RowFetcher``."""

    def __init__(self, fetcher: RowFetcher, *, query: str = DEFAULT_QUERY) -> None:
        self._fetcher = fetcher
        self._query = query

    async def lookup(self, phone_e164: str) -> LookupResult:
        rows = await self._fetcher.fetch_all(self._query, (phone_e164,))
        if not rows:
            return LookupResult.denied(DenyReason.NOT_FOUND)
        if len(rows) > 1:
            return LookupResult.denied(DenyReason.REVIEW_REQUIRED)
        row = rows[0]
        if int(row.get("source_count") or 0) <= 0:
            return LookupResult.denied(DenyReason.NOT_FOUND)
        if _has_review_reasons(row.get("review_reasons")):
            return LookupResult.denied(DenyReason.REVIEW_REQUIRED)
        return LookupResult.found(Customer(str(row["customer_id"]), row.get("display_name")))
