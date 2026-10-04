from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from tg_site_login.directory import (
    Customer,
    DenyReason,
    InMemoryPhoneDirectory,
    LookupResult,
    PhoneDirectory,
)
from tg_site_login.sql_directory import DEFAULT_QUERY, SqlPhoneDirectory


def test_lookup_result_requires_exactly_one_side() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        LookupResult()
    with pytest.raises(ValueError, match="exactly one"):
        LookupResult(customer=Customer("c"), deny_reason=DenyReason.NOT_FOUND)


async def test_in_memory_directory_denial_reasons(directory: InMemoryPhoneDirectory) -> None:
    found = await directory.lookup("+79991234567")
    assert found.customer == Customer("c-1", "Anna")
    assert found.deny_reason is None

    unknown = await directory.lookup("+79995550000")
    assert unknown.customer is None
    assert unknown.deny_reason is DenyReason.NOT_FOUND

    flagged = await directory.lookup("+79990000002")
    assert flagged.deny_reason is DenyReason.REVIEW_REQUIRED

    ambiguous = await directory.lookup("+79990000003")  # two customers share the phone
    assert ambiguous.deny_reason is DenyReason.REVIEW_REQUIRED


async def test_directory_from_json_file(tmp_path: Path) -> None:
    path = tmp_path / "customers.json"
    path.write_text(
        json.dumps(
            [
                {"phone": "+79991230001", "customer_id": "a", "name": "Alice"},
                {"phone": "+79991230002", "customer_id": "b", "review_reasons": ["dup"]},
            ]
        ),
        encoding="utf-8",
    )
    loaded = InMemoryPhoneDirectory.from_json_file(path)
    assert (await loaded.lookup("+79991230001")).customer == Customer("a", "Alice")
    assert (await loaded.lookup("+79991230002")).deny_reason is DenyReason.REVIEW_REQUIRED


def test_in_memory_directory_satisfies_the_protocol(directory: InMemoryPhoneDirectory) -> None:
    assert isinstance(directory, PhoneDirectory)


class FakeFetcher:
    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, Sequence[object]]] = []

    async def fetch_all(self, query: str, params: Sequence[object]) -> Sequence[Mapping[str, Any]]:
        self.calls.append((query, params))
        return self.rows


def row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "customer_id": "c-1",
        "display_name": "Anna",
        "review_reasons": "[]",
        "source_count": 2,
    }
    return base | overrides


@pytest.mark.parametrize(
    ("rows", "customer", "reason"),
    [
        ([], None, DenyReason.NOT_FOUND),
        ([row()], Customer("c-1", "Anna"), None),
        ([row(review_reasons=[])], Customer("c-1", "Anna"), None),
        ([row(review_reasons=None)], Customer("c-1", "Anna"), None),
        ([row(review_reasons='["name_mismatch"]')], None, DenyReason.REVIEW_REQUIRED),
        ([row(review_reasons=["name_mismatch"])], None, DenyReason.REVIEW_REQUIRED),
        ([row(review_reasons="not json")], None, DenyReason.REVIEW_REQUIRED),
        ([row(source_count=0)], None, DenyReason.NOT_FOUND),
        ([row(source_count=None)], None, DenyReason.NOT_FOUND),
        ([row(), row(customer_id="c-2")], None, DenyReason.REVIEW_REQUIRED),
    ],
)
async def test_sql_directory_rules(
    rows: list[dict[str, Any]], customer: Customer | None, reason: DenyReason | None
) -> None:
    fetcher = FakeFetcher(rows)
    result = await SqlPhoneDirectory(fetcher).lookup("+79991234567")
    assert result.customer == customer
    assert result.deny_reason == reason
    assert fetcher.calls == [(DEFAULT_QUERY, ("+79991234567",))]  # parameterized, no inlining


async def test_sql_directory_accepts_a_custom_query() -> None:
    fetcher = FakeFetcher([])
    await SqlPhoneDirectory(fetcher, query="SELECT 1 WHERE :p").lookup("+79991234567")
    assert fetcher.calls[0][0] == "SELECT 1 WHERE :p"
