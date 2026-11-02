"""Late-payment risk baseline for Duebell (issue #12).

The shipped scorer is a rule baseline. It returns a score, a level, and
reasons a human can check. A separate training script compares a logistic
regression on synthetic history, but the demo uses this explainable path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean

LIMITED_DATA_BELOW = 2
LOW_BELOW = 0.30
HIGH_FROM = 0.55


@dataclass(frozen=True)
class HistoryInvoice:
    amount_cents: int
    due_at: datetime
    paid_at: datetime | None = None


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


def extract_features(
    amount_cents: int,
    terms_days: int,
    history: list[HistoryInvoice],
    usual_amount_cents: int | None = None,
    now: datetime | None = None,
) -> dict:
    prior_count = len(history)
    lateness = [days_late(item, now) for item in history]
    late_count = sum(1 for days in lateness if days > 0)
    late_share = late_count / prior_count if prior_count else 0.0
    average_days_late = mean(lateness) if lateness else 0.0
    usual = usual_amount_cents or (int(mean(item.amount_cents for item in history)) if history else amount_cents)
    amount_ratio = amount_cents / usual if usual else 1.0
    return {
        "prior_count": prior_count,
        "late_count": late_count,
        "late_share": late_share,
        "average_days_late": average_days_late,
        "amount_ratio": amount_ratio,
        "terms_days": terms_days,
        "new_client": prior_count == 0,
        "limited_data": prior_count < LIMITED_DATA_BELOW,
    }


def level_for(score: float) -> str:
    if score < LOW_BELOW:
        return "LOW"
    if score < HIGH_FROM:
        return "MEDIUM"
    return "HIGH"


def score_invoice(
    amount_cents: int,
    terms_days: int,
    history: list[HistoryInvoice],
    usual_amount_cents: int | None = None,
    now: datetime | None = None,
) -> RiskAssessment:
    features = extract_features(amount_cents, terms_days, history, usual_amount_cents, now)
    reasons: list[str] = []

    if features["new_client"]:
        score = 0.40
        reasons.append("New client. No payment history yet, so this score is a rough guide.")
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

    if features["amount_ratio"] >= 1.5:
        score += 0.08
        reasons.append(
            f"This invoice is {features['amount_ratio']:.1f}x the client's usual amount."
        )

    if terms_days > 30:
        score += 0.04
        reasons.append(f"Net {terms_days} terms are longer than 30 days.")

    if features["limited_data"] and not features["new_client"]:
        reasons.append("Limited history. Two or more paid invoices would sharpen this score.")

    score = round(max(0.0, min(1.0, score)), 2)
    return RiskAssessment(
        score=score,
        level=level_for(score),
        reasons=reasons,
        limited_data=features["limited_data"],
    )
