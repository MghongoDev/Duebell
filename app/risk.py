"""Late-payment risk baseline for Duebell (issues #12, #23, #46).

The shipped scorer is a rule baseline. It returns a score, a level, and
reasons a human can check. A separate training script compares a logistic
regression on synthetic history, but the demo uses this explainable path.
See ADR 0002.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from statistics import mean

LIMITED_DATA_BELOW = 2
LOW_BELOW = 0.30
HIGH_FROM = 0.55

# Every limited-data reason starts with one of these, so the dashboard can
# find it among the stored reasons (issue #46).
NEW_CLIENT = "New client."
NO_OUTCOME_YET = "No paid or overdue invoices yet."
FIRST_IN_CURRENCY = "First"
LIMITED_HISTORY = "Limited history."
LIMITED_DATA_STARTS = (NEW_CLIENT, NO_OUTCOME_YET, FIRST_IN_CURRENCY, LIMITED_HISTORY)


@dataclass(frozen=True)
class HistoryInvoice:
    amount_cents: int
    due_at: datetime
    paid_at: datetime | None = None
    currency: str = "USD"


@dataclass(frozen=True)
class RiskAssessment:
    score: float
    level: str
    reasons: list[str]
    limited_data: bool


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def days_late(invoice: HistoryInvoice, now: datetime | None = None) -> int:
    end = invoice.paid_at or _now(now)
    return max(0, (end - invoice.due_at).days)


def outcome_known(invoice: HistoryInvoice, now: datetime | None = None) -> bool:
    """True once an invoice's outcome is known at `now` (issue #46).

    Known means paid by `now`, or unpaid and at least one full day past its
    due date. An open invoice that is not yet due says nothing about how the
    client pays, so it never counts as paid on time.
    """
    moment = _now(now)
    if invoice.paid_at is not None and invoice.paid_at <= moment:
        return True
    return max(0, (moment - invoice.due_at).days) > 0


def extract_features(
    amount_cents: int,
    terms_days: int,
    history: list[HistoryInvoice],
    usual_amount_cents: int | None = None,
    now: datetime | None = None,
    currency: str | None = None,
) -> dict:
    # Only invoices whose outcome is known count. Open ones wait (issue #46).
    known = [item for item in history if outcome_known(item, now)]
    open_count = len(history) - len(known)
    prior_count = len(known)
    lateness = [days_late(item, now) for item in known]
    late_count = sum(1 for days in lateness if days > 0)
    late_share = late_count / prior_count if prior_count else 0.0
    average_days_late = mean(lateness) if lateness else 0.0
    # Lateness is behaviour and uses every currency. Amounts only compare
    # within one currency, so a client's EUR history never skews a USD ratio.
    priced = [item for item in known if currency is None or item.currency == currency]
    usual = usual_amount_cents or (int(mean(item.amount_cents for item in priced)) if priced else amount_cents)
    amount_ratio = amount_cents / usual if usual else 1.0
    return {
        "prior_count": prior_count,
        "open_count": open_count,
        "late_count": late_count,
        "late_share": late_share,
        "average_days_late": average_days_late,
        "amount_ratio": amount_ratio,
        "terms_days": terms_days,
        "new_client": prior_count == 0,
        "new_currency": prior_count > 0 and not priced,
        # A first invoice in a new currency has no amount history to compare.
        "limited_data": prior_count < LIMITED_DATA_BELOW or (prior_count > 0 and not priced),
    }


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


def level_for(score: float) -> RiskLevel:
    if score < LOW_BELOW:
        return RiskLevel.LOW
    if score < HIGH_FROM:
        return RiskLevel.MEDIUM
    return RiskLevel.HIGH


def score_invoice(
    amount_cents: int,
    terms_days: int,
    history: list[HistoryInvoice],
    usual_amount_cents: int | None = None,
    now: datetime | None = None,
    currency: str | None = None,
) -> RiskAssessment:
    features = extract_features(amount_cents, terms_days, history, usual_amount_cents, now, currency)
    reasons: list[str] = []

    if features["new_client"]:
        score = 0.40
        waiting = features["open_count"]
        if waiting:
            are = "invoice is" if waiting == 1 else "invoices are"
            reasons.append(f"{NO_OUTCOME_YET} {waiting} open {are} not yet due, so this score is a rough guide.")
        else:
            reasons.append(f"{NEW_CLIENT} No payment history yet, so this score is a rough guide.")
    else:
        score = 0.18
        score += 0.45 * features["late_share"]
        score += 0.35 * min(features["average_days_late"] / 30.0, 1.0)
        prior = features["prior_count"]
        late = features["late_count"]
        invoice_word = "invoice" if prior == 1 else "invoices"
        days = round(features["average_days_late"])
        day_word = "day" if days == 1 else "days"
        if late == 0:
            if prior == 1:
                reasons.append("Paid the only prior invoice on time.")
            else:
                reasons.append(f"Paid all {prior} prior invoices on time.")
        else:
            reasons.append(
                f"Paid {late} of last {prior} {invoice_word} late, average {days} {day_word}."
            )

    if features["new_currency"] and usual_amount_cents is None:
        reasons.append(
            f"{FIRST_IN_CURRENCY} {currency} invoice for this client, so there is no usual amount to compare."
        )
    elif features["amount_ratio"] >= 1.5:
        score += 0.08
        reasons.append(
            f"This invoice is {features['amount_ratio']:.1f}x the client's usual amount."
        )

    if terms_days > 30:
        score += 0.04
        reasons.append(f"Net {terms_days} terms are longer than 30 days.")

    if features["limited_data"] and not features["new_client"] and not features["new_currency"]:
        reasons.append(f"{LIMITED_HISTORY} Two or more paid or overdue invoices would sharpen this score.")

    score = round(max(0.0, min(1.0, score)), 2)
    return RiskAssessment(
        score=score,
        level=level_for(score),
        reasons=reasons,
        limited_data=features["limited_data"],
    )


def limited_data_reason(reasons: list[str]) -> str | None:
    """The reason behind a limited-data score, found among stored reasons (issue #46)."""
    return next((reason for reason in reasons if reason.startswith(LIMITED_DATA_STARTS)), None)
