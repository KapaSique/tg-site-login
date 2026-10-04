from __future__ import annotations

import pytest

from tg_site_login.phone import mask_phone, normalize_phone


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Russian trunk and country forms
        ("89991234567", "+79991234567"),
        ("79991234567", "+79991234567"),
        ("+79991234567", "+79991234567"),
        ("9991234567", "+79991234567"),
        # separators and whitespace
        ("8 (999) 123-45-67", "+79991234567"),
        ("+7 999 123 45 67", "+79991234567"),
        ("+7(999)123.45.67", "+79991234567"),
        ("  8-999-123-45-67\n", "+79991234567"),
        # landline-style Russian numbers
        ("8 (495) 000-00-00", "+74950000000"),
        ("+7 343 000 00 00", "+73430000000"),
        # international numbers, with and without a plus (Telegram sends bare digits)
        ("+380501234567", "+380501234567"),
        ("380501234567", "+380501234567"),
        ("+1 212 555 0100", "+12125550100"),
        ("12125550100", "+12125550100"),
        ("+86 138 0013 8000", "+8613800138000"),
        ("8613800138000", "+8613800138000"),
    ],
)
def test_valid_numbers_are_normalized_to_e164(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "+",
        "abc",
        "8999123456a",
        "12345",  # too short
        "999123456",  # 9 digits
        "+7999123456",  # +7 with 9 national digits
        "+799912345678",  # +7 with 11 national digits
        "7999123456789",  # 7 plus 12 digits
        "89991234",  # too short for a trunk form
        "+7 099 123 45 67",  # national number cannot start with 0
        "+7 199 123 45 67",  # nor with 1 or 2
        "+0123456789",  # leading zero is not a country code
        "00380501234567",  # 00 international prefix is not supported
        "+1234567",  # fewer than 8 digits
        "+1234567890123456",  # more than 15 digits
        "++79991234567",
        "+7999-123-45-67 ext 12",
        "٠٩٩٩١٢٣٤٥٦٧",  # non-ASCII digits
    ],
)
def test_invalid_numbers_are_rejected(raw: str) -> None:
    assert normalize_phone(raw) is None


def test_normalization_is_idempotent() -> None:
    once = normalize_phone("8 (999) 123-45-67")
    assert once is not None
    assert normalize_phone(once) == once


def test_mask_phone_hides_the_middle() -> None:
    assert mask_phone("+79991234567") == "+7***67"
    assert mask_phone("+7") == "***"
