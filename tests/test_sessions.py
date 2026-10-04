from __future__ import annotations

from tg_site_login.directory import Customer
from tg_site_login.sessions import SessionIssuer, StoreSessionIssuer
from tg_site_login.store import MemoryStore


async def test_issued_session_is_opaque_and_stored_hashed(store: MemoryStore) -> None:
    issuer = StoreSessionIssuer(store, cookie_name="sid", ttl_seconds=3600)
    assert isinstance(issuer, SessionIssuer)

    session = await issuer.issue(Customer("c-1", "Anna"))
    assert session.cookie_name == "sid"
    assert session.max_age == 3600
    assert len(session.value) >= 32

    # the raw token is nowhere in the store, only its hash is a key
    assert all(session.value not in key for key in store._data)
    assert await issuer.resolve(session.value) == Customer("c-1", "Anna")
    assert await issuer.resolve("wrong-token") is None


async def test_each_session_gets_its_own_token(store: MemoryStore) -> None:
    issuer = StoreSessionIssuer(store)
    first = await issuer.issue(Customer("c-1"))
    second = await issuer.issue(Customer("c-1"))
    assert first.value != second.value
