"""Proposal validation for Duebell (issues #9, #14, #44)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel

from app.config import Settings
from app.currency import decimal_places, format_amount, to_minor_units

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InvoiceProposal(BaseModel):
    client_name: str = ""
    client_email: str = ""
    description: str = ""
    amount: Decimal = Decimal("0")
    currency: str = "USD"
    terms_days: int = 14


@dataclass(frozen=True)
class FieldError:
    field: str
    message: str


def proposal_to_cents(amount: Decimal, currency: str) -> int:
    """The amount in the currency's minor units: 12.50 USD is 1250, 1000 JPY is 1000 (issue #44)."""
    return to_minor_units(amount, currency)


def validate_proposal(proposal: InvoiceProposal, settings: Settings) -> list[FieldError]:
    errors: list[FieldError] = []

    name = proposal.client_name.strip()
    if not name:
        errors.append(FieldError("client_name", "Client name is required."))
    elif len(name) > 120:
        errors.append(FieldError("client_name", "Client name must be 120 characters or fewer."))

    email = proposal.client_email.strip()
    if not email:
        errors.append(FieldError("client_email", "Client email is required."))
    elif len(email) > 254 or not EMAIL_RE.match(email):
        errors.append(FieldError("client_email", "Enter a valid email address."))

    description = proposal.description.strip()
    if not description:
        errors.append(FieldError("description", "Describe the work."))
    elif len(description) > 500:
        errors.append(FieldError("description", "Description must be 500 characters or fewer."))

    currency = proposal.currency.strip().upper()
    try:
        cents = proposal_to_cents(proposal.amount, currency)
    except (InvalidOperation, ValueError):
        cents = 0
    if cents <= 0:
        errors.append(FieldError("amount", "Amount must be greater than zero."))
    elif decimal_places(currency) == 0 and proposal.amount != proposal.amount.to_integral_value():
        # PayPal refuses decimals in these currencies (issue #44).
        errors.append(FieldError("amount", f"{currency} amounts are whole numbers. Remove the decimals."))
    elif cents > settings.amount_cap_cents:
        cap = format_amount(settings.amount_cap_cents, currency)
        errors.append(FieldError("amount", f"Amount must be at most {cap}."))

    if currency not in settings.allowed_currencies:
        allowed = ", ".join(settings.allowed_currencies)
        errors.append(FieldError("currency", f"Currency must be one of: {allowed}."))

    if proposal.terms_days < settings.terms_min_days or proposal.terms_days > settings.terms_max_days:
        errors.append(
            FieldError(
                "terms_days",
                f"Payment terms must be between {settings.terms_min_days} and {settings.terms_max_days} days.",
            )
        )

    return errors
