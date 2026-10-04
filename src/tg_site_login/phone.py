"""Phone number normalization to E.164 with Russian national conventions."""

from __future__ import annotations

import re

_ALLOWED_SEPARATORS = re.compile(r"[\s\-().]")
_DIGITS = re.compile(r"[0-9]+")

_RU_NATIONAL_LENGTH = 10
_E164_MIN_DIGITS = 8
_E164_MAX_DIGITS = 15


def normalize_phone(raw: str) -> str | None:
    """Return the number in E.164 form (``+79990001122``) or ``None`` if it is not a phone.

    Rules, in order:

    * spaces, dashes, dots and parentheses are dropped; a single leading ``+`` is kept;
    * Russian trunk forms ``8XXXXXXXXXX`` and ``7XXXXXXXXXX`` and the 10-digit mobile
      form ``9XXXXXXXXX`` become ``+7XXXXXXXXXX``;
    * a number with an explicit ``+`` and country code 7 must have exactly 10 national
      digits, the first of which is 3-9;
    * any other ``+`` number is accepted if it has 8-15 digits and no leading zero;
    * Telegram sends contacts as bare international digits (no ``+``), so 11-15 bare
      digits that are not a Russian form are treated as international as well.

    Known ambiguity: a bare 11-digit number starting with 8 is read as a Russian trunk
    form, so a foreign number such as a Vietnamese ``84...`` written without ``+`` is
    misread as Russian. Numbers written with ``+`` are never ambiguous.
    """
    cleaned = _ALLOWED_SEPARATORS.sub("", raw.strip())
    has_plus = cleaned.startswith("+")
    digits = cleaned[1:] if has_plus else cleaned
    if not digits or not _DIGITS.fullmatch(digits):
        return None

    if has_plus:
        if digits.startswith("7"):
            return _ru(digits[1:])
        return _international(digits)

    length = len(digits)
    if length == _RU_NATIONAL_LENGTH and digits[0] == "9":
        return _ru(digits)
    if length == _RU_NATIONAL_LENGTH + 1 and digits[0] in "78":
        return _ru(digits[1:])
    if digits[0] == "7":
        return None
    return _international(digits) if length > _RU_NATIONAL_LENGTH else None


def _ru(national: str) -> str | None:
    if len(national) != _RU_NATIONAL_LENGTH or national[0] not in "3456789":
        return None
    return f"+7{national}"


def _international(digits: str) -> str | None:
    if digits[0] == "0" or not _E164_MIN_DIGITS <= len(digits) <= _E164_MAX_DIGITS:
        return None
    return f"+{digits}"


def mask_phone(e164: str) -> str:
    """Mask a phone for logs: keep the country prefix and the last two digits."""
    if len(e164) < 6:
        return "***"
    return f"{e164[:2]}***{e164[-2:]}"
