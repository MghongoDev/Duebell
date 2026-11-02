"""Proposal validation for Duebell (issues #9, #14)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from pydantic import BaseModel, Field

from app.config import Settings

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


def proposal_to_cents(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def validate_proposal(proposal: InvoiceProposal, settings: Settings) -> list[FieldError]:
    errors: list[FieldError] = []

    if not proposal.client_name.strip():
        errors.append(FieldError("client_name", "Client name is required."))

    email = proposal.client_email.strip()
    if not email:
        errors.append(FieldError("client_email", "Client email is required."))
    elif not EMAIL_RE.match(email):
        errors.append(FieldError("client_email", "Enter a valid email address."))

    if not proposal.description.strip():
        errors.append(FieldError("description", "Describe the work."))

    try:
        cents = proposal_to_cents(proposal.amount)
    except (InvalidOperation, ValueError):
        cents = 0
    if cents <= 0:
        errors.append(FieldError("amount", "Amount must be greater than zero."))
    elif cents > settings.amount_cap_cents:
        cap = settings.amount_cap_cents / 100
        errors.append(FieldError("amount", f"Amount must be at most {cap:,.2f}."))

    currency = proposal.currency.strip().upper()
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
