"""Helpers for Telegram HTML messages: escaping, truncation and length-safe splitting."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from enum import Enum

TELEGRAM_TEXT_LIMIT = 4096


def escape(value: str) -> str:
    """Escape user-controlled text for Telegram's HTML parse mode (``& < >``)."""
    return html.escape(value, quote=False)


def escape_attr(value: str) -> str:
    """Escape a value placed inside an HTML attribute such as ``href``."""
    return html.escape(value, quote=True)


def truncate(value: str, max_chars: int, suffix: str = "…") -> str:
    """Cut raw (unescaped) text to ``max_chars`` characters, ending with ``suffix``.

    Truncate before escaping: cutting escaped text could split an entity like ``&amp;``.
    """
    if len(value) <= max_chars:
        return value
    if max_chars <= len(suffix):
        return suffix[:max_chars]
    return value[: max_chars - len(suffix)] + suffix


class _Kind(Enum):
    OPEN = "open"
    CLOSE = "close"
    TEXT = "text"


@dataclass(frozen=True, slots=True)
class _Token:
    text: str
    kind: _Kind
    name: str = ""
    width: int = 0


_TOKEN_RE = re.compile(
    r"<(?P<close>/?)(?P<name>[A-Za-z][A-Za-z0-9-]*)(?:\s[^<>]*)?>"
    r"|(?P<entity>&(?:#[0-9]+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);)"
    r"|(?P<char>[\s\S])"
)


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    for match in _TOKEN_RE.finditer(text):
        if match.group("name"):
            kind = _Kind.CLOSE if match.group("close") else _Kind.OPEN
            tokens.append(_Token(match.group(0), kind, name=match.group("name")))
        elif match.group("entity"):
            tokens.append(_Token(match.group(0), _Kind.TEXT, width=1))
        else:
            char = match.group("char")
            tokens.append(_Token(char, _Kind.TEXT, width=2 if ord(char) > 0xFFFF else 1))
    return tokens


def visible_length(text: str) -> int:
    """Length Telegram counts for the 4096 limit: tags are free, entities are one character.

    Characters outside the Basic Multilingual Plane count as two (UTF-16 code units),
    which is the conservative reading of "characters after entities parsing".
    """
    return sum(token.width for token in _tokenize(text))


def _closers(open_tags: list[_Token]) -> str:
    return "".join(f"</{tag.name}>" for tag in reversed(open_tags))


def split_html(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split Telegram-HTML ``text`` into chunks whose visible length is at most ``limit``.

    Breaks prefer the last newline inside a chunk; otherwise the text is cut between
    characters, never inside a tag or an entity. Tags that are open at a cut are closed
    at the end of the chunk and reopened at the start of the next one, so every chunk is
    valid HTML on its own.
    """
    if limit < 2:
        msg = "limit must be at least 2"
        raise ValueError(msg)
    tokens = _tokenize(text)
    if sum(token.width for token in tokens) <= limit:
        return [text]

    chunks: list[str] = []
    open_tags: list[_Token] = []
    prefix_tags: list[_Token] = []
    start = 0
    width = 0
    last_newline: tuple[int, list[_Token]] | None = None  # (token index, open tags there)
    i = 0

    def emit(end: int, tags_at_end: list[_Token]) -> None:
        body = "".join(tok.text for tok in tokens[start:end])
        chunks.append("".join(tok.text for tok in prefix_tags) + body + _closers(tags_at_end))

    while i < len(tokens):
        token = tokens[i]
        if token.kind is _Kind.OPEN:
            open_tags.append(token)
        elif token.kind is _Kind.CLOSE:
            for pos in range(len(open_tags) - 1, -1, -1):
                if open_tags[pos].name == token.name:
                    del open_tags[pos:]
                    break
        else:
            if width + token.width > limit:
                if last_newline is not None:
                    newline_index, tags_there = last_newline
                    emit(newline_index, tags_there)
                    i = newline_index + 1
                    open_tags = list(tags_there)
                else:
                    emit(i, open_tags)
                    # re-process the current token in the next chunk
                start = i
                prefix_tags = list(open_tags)
                width = 0
                last_newline = None
                continue
            if token.text == "\n" and width > 0:
                last_newline = (i, list(open_tags))
            width += token.width
        i += 1

    if start < len(tokens):
        emit(len(tokens), open_tags)
    return [chunk for chunk in chunks if visible_length(chunk) > 0]
