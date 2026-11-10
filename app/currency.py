"""Decimal places per currency (issue #44).

Every amount_cents field holds the currency's minor units: cents for USD,
whole yen for JPY. So 12.50 USD is stored as 1250 and 1000 JPY as 1000.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# PayPal's currencies with no decimals. PayPal returns an error for a decimal
# amount in these. Source: https://developer.paypal.com/api/rest/reference/currency-codes/
ZERO_DECIMAL_CURRENCIES = frozenset({"HUF", "JPY", "TWD"})


def decimal_places(currency: str) -> int:
    """How many decimal places the currency allows: 0 or 2."""
    return 0 if currency.strip().upper() in ZERO_DECIMAL_CURRENCIES else 2


def to_minor_units(amount: Decimal, currency: str) -> int:
    """Turn an amount such as 12.50 into minor units, rounding half up."""
    return int((amount * 10 ** decimal_places(currency)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_minor_units(minor: int, currency: str) -> Decimal:
    """Turn minor units back into an amount, such as 1250 USD into 12.50."""
    return Decimal(minor).scaleb(-decimal_places(currency))


def paypal_value(minor: int, currency: str) -> str:
    """The amount as PayPal's value string: "1200.00" for USD, "1000" for JPY."""
    places = decimal_places(currency)
    return f"{minor / 10 ** places:.{places}f}"


def format_amount(minor: int, currency: str) -> str:
    """The amount for display, with thousands separators and no currency code."""
    places = decimal_places(currency)
    return f"{minor / 10 ** places:,.{places}f}"
