"""The login state machine: lifecycle, one-time use, nonce binding, spoofed contacts."""

from __future__ import annotations

import asyncio
import hashlib
import time

import pytest

from tests.conftest import make_login
from tests.support import FakeClock, new_redis
from tg_site_login.directory import Customer, DenyReason, InMemoryPhoneDirectory, LookupResult
from tg_site_login.login import (
    ClaimStatus,
    LoginDisabledError,
    LoginService,
    PollStatus,
    SubmitStatus,
)
from tg_site_login.redis_store import RedisStore
from tg_site_login.sessions import IssuedSession, StoreSessionIssuer
from tg_site_login.store import MemoryStore

ANNA = 111  # Telegram user id of a customer
PHONE = "8 (999) 123-45-67"  # Anna's phone as Telegram or a human might write it


async def confirm(
    login: LoginService, code: str, tg_user_id: int = ANNA, phone: str = PHONE
) -> None:
    assert (await login.claim(code, tg_user_id)).status is ClaimStatus.OK
    result = await login.submit_contact(tg_user_id, tg_user_id, phone)
    assert result.status is SubmitStatus.CONFIRMED


async def test_happy_path_issues_one_session(login: LoginService) -> None:
    started = await login.start()
    assert (await login.poll(started.nonce)).status is PollStatus.PENDING

    claim = await login.claim(started.code, ANNA)
    assert claim.status is ClaimStatus.OK
    assert claim.pairing_code == started.pairing_code
    assert (await login.poll(started.nonce)).status is PollStatus.PENDING  # not confirmed yet

    submitted = await login.submit_contact(ANNA, ANNA, PHONE)
    assert submitted.status is SubmitStatus.CONFIRMED
    assert submitted.customer == Customer("c-1", "Anna")

    polled = await login.poll(started.nonce)
    assert polled.status is PollStatus.CONFIRMED
    assert isinstance(polled.session, IssuedSession)
    assert polled.deny_reason is None


async def test_code_is_one_time(login: LoginService) -> None:
    started = await login.start()
    await confirm(login, started.code)
    assert (await login.poll(started.nonce)).status is PollStatus.CONFIRMED
    # the code is burned: neither polling nor the deep link works a second time
    again = await login.poll(started.nonce)
    assert again.status is PollStatus.EXPIRED
    assert again.session is None
    assert (await login.claim(started.code, ANNA)).status is ClaimStatus.INVALID


async def test_concurrent_polls_issue_a_single_session(
    store: MemoryStore, directory: InMemoryPhoneDirectory, clock: FakeClock
) -> None:
    issued: list[Customer] = []

    class CountingIssuer:
        async def issue(self, customer: Customer) -> IssuedSession:
            issued.append(customer)
            await asyncio.sleep(0)
            return IssuedSession("session", f"token-{len(issued)}", 60)

    login = LoginService(store, directory, CountingIssuer(), ttl=300, clock=clock)
    started = await login.start()
    await confirm(login, started.code)

    results = await asyncio.gather(*(login.poll(started.nonce) for _ in range(8)))
    statuses = [r.status for r in results]
    assert statuses.count(PollStatus.CONFIRMED) == 1
    assert statuses.count(PollStatus.EXPIRED) == 7
    assert len(issued) == 1


async def test_code_expires_after_ttl(login: LoginService, clock: FakeClock) -> None:
    started = await login.start()
    clock.advance(299)
    assert (await login.poll(started.nonce)).status is PollStatus.PENDING
    clock.advance(2)
    assert (await login.poll(started.nonce)).status is PollStatus.EXPIRED
    assert (await login.claim(started.code, ANNA)).status is ClaimStatus.INVALID


async def test_confirmation_after_expiry_does_not_log_in(
    login: LoginService, clock: FakeClock
) -> None:
    started = await login.start()
    assert (await login.claim(started.code, ANNA)).status is ClaimStatus.OK
    clock.advance(301)
    result = await login.submit_contact(ANNA, ANNA, PHONE)
    assert result.status is SubmitStatus.NO_ACTIVE_CODE
    polled = await login.poll(started.nonce)
    assert polled.status is PollStatus.EXPIRED
    assert polled.session is None


async def test_expires_at_is_ttl_from_now(login: LoginService, clock: FakeClock) -> None:
    started = await login.start()
    assert started.expires_at == clock.now + 300
    assert len(started.pairing_code) == 4
    assert started.pairing_code.isdigit()


async def test_nonce_mismatch_gets_no_session_and_does_not_burn_the_code(
    login: LoginService,
) -> None:
    started = await login.start()
    await confirm(login, started.code)

    for wrong in ("not-the-nonce", started.code, "x" * 500, "", None):
        polled = await login.poll(wrong)
        assert polled.status is PollStatus.EXPIRED  # indistinguishable from "no such code"
        assert polled.session is None

    # the legitimate browser is unaffected by the failed attempts
    assert (await login.poll(started.nonce)).status is PollStatus.CONFIRMED


async def test_two_browsers_cannot_read_each_others_attempt(login: LoginService) -> None:
    first = await login.start()
    second = await login.start()
    await confirm(login, first.code)

    assert (await login.poll(second.nonce)).status is PollStatus.PENDING
    assert (await login.poll(first.nonce)).status is PollStatus.CONFIRMED


async def test_starting_again_revokes_the_previous_attempt_of_the_same_browser(
    login: LoginService,
) -> None:
    old = await login.start()
    new = await login.start(previous_nonce=old.nonce)
    assert (await login.poll(old.nonce)).status is PollStatus.EXPIRED
    assert (await login.claim(old.code, ANNA)).status is ClaimStatus.INVALID
    assert (await login.poll(new.nonce)).status is PollStatus.PENDING


async def test_codes_and_nonces_are_unique_and_unguessable(login: LoginService) -> None:
    attempts = [await login.start() for _ in range(50)]
    assert len({a.code for a in attempts}) == 50
    assert len({a.nonce for a in attempts}) == 50
    assert all(len(a.code) >= 32 and len(a.nonce) >= 43 for a in attempts)
    assert all(a.code != a.nonce for a in attempts)


async def test_secrets_are_not_stored_in_plain_text(
    login: LoginService, store: MemoryStore
) -> None:
    started = await login.start()
    await confirm(login, started.code)
    haystack = " ".join(f"{key} {value}" for key, (value, _) in store._data.items())
    assert started.code not in haystack
    assert started.nonce not in haystack
    assert "+79991234567" not in haystack  # the phone is never persisted


async def test_corrupted_nonce_index_cannot_leak_another_attempt(
    login: LoginService, store: MemoryStore
) -> None:
    """Defence in depth: the record itself must also carry the matching nonce hash."""
    mine = await login.start()
    other = await login.start()
    await confirm(login, other.code)
    other_hash = hashlib.sha256(other.code.encode()).hexdigest()
    mine_nonce_hash = hashlib.sha256(mine.nonce.encode()).hexdigest()
    # a bug or a tampered store points my nonce at someone else's confirmed attempt
    await store.set(f"login:nonce:{mine_nonce_hash}", other_hash, 300)

    polled = await login.poll(mine.nonce)
    assert polled.status is PollStatus.EXPIRED
    assert polled.session is None


async def test_expiry_is_enforced_by_the_service_even_if_the_store_keeps_the_key(
    directory: InMemoryPhoneDirectory, clock: FakeClock
) -> None:
    sloppy_store = MemoryStore(clock=lambda: 0.0)  # its TTLs never fire
    login = make_login(sloppy_store, directory, clock)
    started = await login.start()
    await confirm(login, started.code)
    clock.advance(301)
    polled = await login.poll(started.nonce)
    assert polled.status is PollStatus.EXPIRED
    assert polled.session is None
    assert (await login.claim(started.code, ANNA)).status is ClaimStatus.INVALID


async def test_racing_contacts_cannot_flip_the_outcome(
    store: MemoryStore, directory: InMemoryPhoneDirectory, clock: FakeClock
) -> None:
    class SlowDirectory:
        async def lookup(self, phone_e164: str) -> LookupResult:
            await asyncio.sleep(0)  # lets the other submit run between check and write
            return await directory.lookup(phone_e164)

    login = LoginService(store, SlowDirectory(), StoreSessionIssuer(store), ttl=300, clock=clock)
    started = await login.start()
    await login.claim(started.code, ANNA)

    results = await asyncio.gather(
        login.submit_contact(ANNA, ANNA, "+7 999 555 00 00"),  # not found
        login.submit_contact(ANNA, ANNA, PHONE),  # found
    )

    statuses = sorted(r.status.value for r in results)
    assert SubmitStatus.ALREADY_FINISHED.value in statuses  # exactly one writer won
    winner = next(r for r in results if r.status is not SubmitStatus.ALREADY_FINISHED)
    polled = await login.poll(started.nonce)
    if winner.status is SubmitStatus.CONFIRMED:
        assert polled.status is PollStatus.CONFIRMED
    else:
        assert polled.status is PollStatus.DENIED


# -- spoofed contacts ------------------------------------------------------------------------


async def test_foreign_contact_is_rejected_and_login_stays_pending(login: LoginService) -> None:
    started = await login.start()
    assert (await login.claim(started.code, 222)).status is ClaimStatus.OK
    # user 222 shares the contact of user 111 (Anna's real phone, Anna's user id)
    result = await login.submit_contact(222, ANNA, PHONE)
    assert result.status is SubmitStatus.FOREIGN_CONTACT
    assert result.customer is None
    polled = await login.poll(started.nonce)
    assert polled.status is PollStatus.PENDING
    assert polled.session is None


async def test_contact_without_a_telegram_account_is_rejected(login: LoginService) -> None:
    started = await login.start()
    await login.claim(started.code, 222)
    result = await login.submit_contact(222, None, PHONE)
    assert result.status is SubmitStatus.FOREIGN_CONTACT
    assert (await login.poll(started.nonce)).status is PollStatus.PENDING


async def test_foreign_attempt_can_be_followed_by_the_real_contact(login: LoginService) -> None:
    started = await login.start()
    await login.claim(started.code, ANNA)
    assert (await login.submit_contact(ANNA, 999, PHONE)).status is SubmitStatus.FOREIGN_CONTACT
    assert (await login.submit_contact(ANNA, ANNA, PHONE)).status is SubmitStatus.CONFIRMED


async def test_code_cannot_be_hijacked_by_a_second_telegram_user(login: LoginService) -> None:
    started = await login.start()
    assert (await login.claim(started.code, ANNA)).status is ClaimStatus.OK
    assert (await login.claim(started.code, 222)).status is ClaimStatus.TAKEN
    # the intruder has no active code, so their contact cannot confirm anything
    assert (await login.submit_contact(222, 222, "+79990000002")).status is (
        SubmitStatus.NO_ACTIVE_CODE
    )
    # the rightful user can open the link again (e.g. after restarting the chat)
    assert (await login.claim(started.code, ANNA)).status is ClaimStatus.OK


async def test_contact_without_opening_a_code_is_ignored(login: LoginService) -> None:
    result = await login.submit_contact(ANNA, ANNA, PHONE)
    assert result.status is SubmitStatus.NO_ACTIVE_CODE


# -- directory outcomes ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phone", "reason"),
    [
        ("+7 999 555 00 00", DenyReason.NOT_FOUND),
        ("8 999 000 00 02", DenyReason.REVIEW_REQUIRED),  # record flagged for review
        ("8 999 000 00 03", DenyReason.REVIEW_REQUIRED),  # two customers share the phone
        ("garbage", DenyReason.NOT_FOUND),  # cannot even be normalized
    ],
)
async def test_denied_logins_report_a_reason_and_never_a_session(
    login: LoginService, phone: str, reason: DenyReason
) -> None:
    started = await login.start()
    await login.claim(started.code, ANNA)

    result = await login.submit_contact(ANNA, ANNA, phone)
    assert result.status is SubmitStatus.DENIED
    assert result.deny_reason is reason

    polled = await login.poll(started.nonce)
    assert polled.status is PollStatus.DENIED
    assert polled.deny_reason is reason
    assert polled.session is None


async def test_outcome_is_written_once(login: LoginService) -> None:
    started = await login.start()
    await login.claim(started.code, ANNA)
    assert (await login.submit_contact(ANNA, ANNA, "+7 999 555 00 00")).status is (
        SubmitStatus.DENIED
    )
    # a second try with a valid phone cannot overwrite the denial
    assert (await login.submit_contact(ANNA, ANNA, PHONE)).status is SubmitStatus.ALREADY_FINISHED
    assert (await login.poll(started.nonce)).status is PollStatus.DENIED
    assert (await login.claim(started.code, ANNA)).status is ClaimStatus.FINISHED


# -- abuse limits and kill switch ------------------------------------------------------------


@pytest.mark.parametrize(
    "payload", ["", "short", "has spaces in it 0123456789", "x" * 80, "ü" * 20]
)
async def test_malformed_start_payloads_are_invalid(login: LoginService, payload: str) -> None:
    assert (await login.claim(payload, ANNA)).status is ClaimStatus.INVALID


async def test_bad_codes_are_rate_limited_per_telegram_user(
    store: MemoryStore, directory: InMemoryPhoneDirectory, clock: FakeClock
) -> None:
    login = make_login(store, directory, clock, bad_code_limit=3)
    guess = "A" * 32
    statuses = [(await login.claim(guess, 666)).status for _ in range(5)]
    assert statuses == [
        ClaimStatus.INVALID,
        ClaimStatus.INVALID,
        ClaimStatus.INVALID,
        ClaimStatus.RATE_LIMITED,
        ClaimStatus.RATE_LIMITED,
    ]
    # other users are not affected, and neither is a valid code of the blocked user
    assert (await login.claim(guess, 777)).status is ClaimStatus.INVALID
    started = await login.start()
    assert (await login.claim(started.code, 666)).status is ClaimStatus.OK


async def test_kill_switch_blocks_every_entry_point(
    store: MemoryStore, directory: InMemoryPhoneDirectory, clock: FakeClock
) -> None:
    login = make_login(store, directory, clock, enabled=False)
    assert login.enabled is False
    with pytest.raises(LoginDisabledError):
        await login.start()
    with pytest.raises(LoginDisabledError):
        await login.poll("nonce")
    with pytest.raises(LoginDisabledError):
        await login.claim("A" * 32, ANNA)
    with pytest.raises(LoginDisabledError):
        await login.submit_contact(ANNA, ANNA, PHONE)


# -- the same lifecycle on Redis -------------------------------------------------------------


async def test_full_flow_on_redis(directory: InMemoryPhoneDirectory) -> None:
    client = new_redis()
    try:
        login = make_login(RedisStore(client), directory, FakeClock(time.time()))
        started = await login.start()
        assert (await login.poll(started.nonce)).status is PollStatus.PENDING
        await confirm(login, started.code)
        assert (await login.poll("wrong")).status is PollStatus.EXPIRED
        polled = await login.poll(started.nonce)
        assert polled.status is PollStatus.CONFIRMED
        assert polled.session is not None
        assert (await login.poll(started.nonce)).status is PollStatus.EXPIRED
        left = await client.keys("tgsl:login:*")
        # the burn removes the record, the nonce index, the claim and the outcome; only the
        # per-user pointer stays, and it expires on its own and points at a dead record
        assert left == ["tgsl:login:user:111"]
        assert await client.pttl("tgsl:login:user:111") > 0
    finally:
        await client.aclose()
