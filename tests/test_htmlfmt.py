from __future__ import annotations

import re

import pytest

from tg_site_login.htmlfmt import (
    TELEGRAM_TEXT_LIMIT,
    escape,
    escape_attr,
    split_html,
    truncate,
    visible_length,
)

_TAG = re.compile(r"<(/?)([a-z]+)[^>]*>")
_ENTITY = re.compile(r"&(?:#[0-9]+|#x[0-9a-fA-F]+|[a-zA-Z]+);")


def test_escape_neutralizes_markup() -> None:
    assert escape('<b>"Tom" & Jerry</b>') == '&lt;b&gt;"Tom" &amp; Jerry&lt;/b&gt;'
    assert escape_attr('x" onclick="y') == "x&quot; onclick=&quot;y"


def test_truncate() -> None:
    assert truncate("short", 10) == "short"
    assert truncate("a" * 20, 10) == "a" * 9 + "…"
    assert truncate("abcdef", 2, suffix="...") == ".."  # never longer than the limit
    assert len(truncate("a" * 20, 10)) == 10


def test_visible_length_ignores_tags_and_counts_entities_once() -> None:
    assert visible_length("<b>ab</b>") == 2
    assert visible_length("a &amp; b") == 5
    assert visible_length('<a href="https://shop.example/?a=1&amp;b=2">link</a>') == 4
    assert visible_length("\U0001f600") == 2  # outside the BMP: two UTF-16 code units


def _balanced(chunk: str) -> bool:
    stack: list[str] = []
    for closing, name in _TAG.findall(chunk):
        if closing:
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


def _plain(chunk: str) -> str:
    return _TAG.sub("", chunk)


def test_short_text_is_returned_unchanged() -> None:
    assert split_html("<b>hi</b>") == ["<b>hi</b>"]


def test_split_prefers_newlines() -> None:
    lines = ["line %03d" % i for i in range(100)]  # noqa: UP031
    chunks = split_html("\n".join(lines), limit=100)
    assert all(visible_length(c) <= 100 for c in chunks)
    assert all(not c.startswith("\n") and not c.endswith("\n") for c in chunks)
    assert "\n".join(chunks).split("\n") == lines  # no line was cut in half


def test_split_hard_cuts_a_single_long_line() -> None:
    chunks = split_html("x" * 10_000, limit=4096)
    assert [len(c) for c in chunks] == [4096, 4096, 1808]


def test_split_never_cuts_inside_an_entity() -> None:
    text = "&amp;" * 50  # visible length 50
    chunks = split_html(text, limit=7)
    assert all(visible_length(c) <= 7 for c in chunks)
    assert "".join(chunks) == text
    for chunk in chunks:
        assert re.fullmatch(r"(?:&amp;)+", chunk)


def test_split_reopens_tags_in_the_next_chunk() -> None:
    text = "<b>" + "word " * 40 + "</b>"
    chunks = split_html(text, limit=50)
    assert len(chunks) > 1
    for chunk in chunks:
        assert visible_length(chunk) <= 50
        assert _balanced(chunk)
        assert chunk.startswith("<b>")
        assert chunk.endswith("</b>")


def test_split_handles_nested_tags_and_attributes() -> None:
    text = '<b>bold <a href="https://shop.example/x?a=1&amp;b=2">' + "link " * 30 + "</a> tail</b>"
    chunks = split_html(text, limit=40)
    assert len(chunks) > 1
    for chunk in chunks:
        assert visible_length(chunk) <= 40
        assert _balanced(chunk)
    assert all('href="https://shop.example/x?a=1&amp;b=2"' in c for c in chunks if "<a " in c)
    assert re.sub(r"\s+", "", _plain("".join(chunks))) == re.sub(r"\s+", "", _plain(text))


def test_split_big_realistic_message() -> None:
    rows = [f"• Item {i} &amp; <code>SKU-{i}</code> — 1 000 ₽" for i in range(400)]
    text = "<b>Order</b>\n" + "\n".join(rows)
    chunks = split_html(text, TELEGRAM_TEXT_LIMIT)
    assert len(chunks) >= 2
    assert all(visible_length(c) <= TELEGRAM_TEXT_LIMIT for c in chunks)
    assert all(_balanced(c) for c in chunks)
    assert "\n".join(chunks).count("SKU-") == 400  # every row survived exactly once


def test_split_rejects_tiny_limit() -> None:
    with pytest.raises(ValueError, match="limit"):
        split_html("abc", limit=1)


def test_every_entity_in_chunks_is_complete() -> None:
    text = "a&lt;b" * 2000
    for chunk in split_html(text, limit=100):
        # removing the complete entities leaves no stray ampersand or semicolon fragments
        assert "&" not in _ENTITY.sub("", chunk)
