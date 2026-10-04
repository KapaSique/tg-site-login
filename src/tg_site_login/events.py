"""Notification events. Each one knows its dedupe key and renders itself as Telegram HTML.

Every user-controlled value goes through ``escape`` (after ``truncate``), so a customer
who types ``<b>`` into a comment cannot break the message or inject markup.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from tg_site_login.htmlfmt import escape, truncate

_CURRENCY_SYMBOLS = {"RUB": "₽", "USD": "$", "EUR": "€"}
_SHORT = 200
_LONG = 1000


@runtime_checkable
class NotificationEvent(Protocol):
    """Anything the ``Notifier`` can send."""

    @property
    def dedupe_key(self) -> str:
        """Stable identity of the event: the same key is never delivered twice."""
        ...

    def render(self) -> str:
        """Telegram HTML (``parse_mode=HTML``); every dynamic value must be escaped."""
        ...


def format_money(amount: Decimal, currency: str) -> str:
    text = f"{amount:,.2f}".replace(",", " ")
    text = text.removesuffix(".00")
    return f"{text} {_CURRENCY_SYMBOLS.get(currency, currency)}"


def _line(label: str, value: str | None, limit: int = _SHORT) -> str | None:
    if not value:
        return None
    return f"{label}: {escape(truncate(value, limit))}"


def _phone_line(phone: str | None) -> str | None:
    if not phone:
        return None
    return f"Телефон: <code>{escape(truncate(phone, 32))}</code>"


def _title(text: str, order_id: str) -> str:
    return f"<b>{text} №{escape(truncate(order_id, 64))}</b>"


def _join(lines: list[str | None]) -> str:
    return "\n".join(line for line in lines if line)


@dataclass(frozen=True, slots=True)
class OrderItem:
    title: str
    quantity: int
    unit_price: Decimal


@dataclass(frozen=True, slots=True)
class OrderCreated:
    order_id: str
    customer_name: str
    items: tuple[OrderItem, ...]
    currency: str = "RUB"
    phone: str | None = None
    payment_method: str | None = None
    comment: str | None = None

    @property
    def total(self) -> Decimal:
        return sum((item.unit_price * item.quantity for item in self.items), Decimal(0))

    @property
    def dedupe_key(self) -> str:
        return f"order-created:{self.order_id}"

    def render(self) -> str:
        rows = [
            f"• {escape(truncate(item.title, _SHORT))}, {item.quantity} шт."
            f" — {format_money(item.unit_price * item.quantity, self.currency)}"
            for item in self.items
        ]
        total = escape(format_money(self.total, self.currency))
        return _join(
            [
                _title("Новый заказ", self.order_id),
                _line("Покупатель", self.customer_name),
                _phone_line(self.phone),
                _line("Оплата", self.payment_method),
                *rows,
                f"Итого: <b>{total}</b>",
                _line("Комментарий", self.comment, _LONG),
            ]
        )


@dataclass(frozen=True, slots=True)
class LeadCreated:
    lead_id: str
    name: str
    phone: str | None = None
    topic: str | None = None
    message: str | None = None
    source: str | None = None

    @property
    def dedupe_key(self) -> str:
        return f"lead-created:{self.lead_id}"

    def render(self) -> str:
        topic = escape(truncate(self.topic or "без темы", _SHORT))
        return _join(
            [
                f"<b>Новая заявка</b> ({topic})",
                _line("Имя", self.name),
                _phone_line(self.phone),
                _line("Источник", self.source),
                _line("Сообщение", self.message, _LONG * 3),
            ]
        )


@dataclass(frozen=True, slots=True)
class OrderUnpaid:
    """Reminder: an order has been waiting for payment."""

    order_id: str
    customer_name: str
    total: Decimal
    waiting_minutes: int
    currency: str = "RUB"
    phone: str | None = None
    payment_method: str | None = None

    @property
    def dedupe_key(self) -> str:
        return f"order-unpaid:{self.order_id}"

    def render(self) -> str:
        total = escape(format_money(self.total, self.currency))
        return _join(
            [
                _title("Заказ не оплачен", self.order_id),
                f"Ждёт оплаты уже {self.waiting_minutes} мин.",
                _line("Покупатель", self.customer_name),
                _phone_line(self.phone),
                _line("Способ оплаты", self.payment_method),
                f"Сумма: <b>{total}</b>",
            ]
        )


@dataclass(frozen=True, slots=True)
class PlainTextEvent:
    """Free-form text. The text is escaped, so it is always safe to pass user input."""

    key: str
    text: str

    @property
    def dedupe_key(self) -> str:
        return f"text:{self.key}"

    def render(self) -> str:
        return escape(self.text)
