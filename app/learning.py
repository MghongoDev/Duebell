"""Refit the risk comparison on the app's own payment history (issues #22, #46, #148; ADRs 0002, 0008).

The rule baseline stays the shipped score (ADR 0002). This module fits a
logistic regression on invoices whose outcome is known and stores a held-out
comparison beside the baseline, so the user can see whether real history
says anything the hand-tuned weights miss. The held-out invoices are the
newest quarter by issue date, and the model trains only on invoices issued
before all of them (issue #148).

Each invoice becomes one row. Its features are computed as of its issue date
from the client's earlier invoices only, so the model never sees a payment
that had not happened yet.
"""

from __future__ import annotations

import math
from datetime import datetime

from sqlmodel import Session, select

from app.models import ComparisonStatus, Invoice, RiskComparison, as_utc, utcnow
from app.ownership import EVERY_USER, Scope, check_scope, owned
from app.risk import HIGH_FROM, HistoryInvoice, days_late, extract_features, outcome_known, score_invoice
from app.services import NOT_HISTORY, history_as_of

FEATURES = ["late_share", "average_days_late", "amount_ratio", "terms_days", "new_client"]
MIN_ROWS = 30
MIN_PER_CLASS = 5
TEST_SHARE = 0.25


def outcome(invoice: Invoice, now: datetime) -> int | None:
    """1 if paid late or unpaid past due, 0 if paid on time, None if unknown.

    Uses the same known-outcome rule as a client's history (issue #46).
    """
    if invoice.status in NOT_HISTORY:
        return None
    paid = as_utc(invoice.paid_at)
    item = HistoryInvoice(
        amount_cents=invoice.amount_cents,
        due_at=as_utc(invoice.due_at),
        paid_at=paid if paid is not None and paid <= now else None,
    )
    if not outcome_known(item, now):
        return None
    return 1 if days_late(item, now) > 0 else 0


def training_rows(session: Session, now: datetime | None = None, *, scope: Scope) -> list[dict]:
    now = as_utc(now or utcnow())
    invoices = list(session.exec(owned(select(Invoice), Invoice, scope)).all())
    by_client: dict[int, list[Invoice]] = {}
    for invoice in invoices:
        by_client.setdefault(invoice.client_id, []).append(invoice)

    rows = []
    for invoice in invoices:
        label = outcome(invoice, now)
        if label is None:
            continue
        as_of = as_utc(invoice.issued_at)
        history = history_as_of(by_client[invoice.client_id], as_of)
        features = extract_features(
            invoice.amount_cents, invoice.terms_days, history, now=as_of, currency=invoice.currency
        )
        rule = score_invoice(
            invoice.amount_cents, invoice.terms_days, history, now=as_of, currency=invoice.currency
        )
        row = {name: float(features[name]) for name in FEATURES}
        row["rule_score"] = rule.score
        row["late"] = label
        row["issued_at"] = as_of
        rows.append(row)
    return rows


def refit(session: Session, now: datetime | None = None, *, user_id: int) -> RiskComparison:
    """Refit on one user's invoices. Users never train on each other's clients."""
    check_scope(user_id)
    if user_id is EVERY_USER:
        raise TypeError("A refit trains on one user's invoices, never on everyone's.")
    rows = training_rows(session, now, scope=user_id)
    late = sum(row["late"] for row in rows)
    on_time = len(rows) - late
    result = RiskComparison(user_id=user_id, rows_used=len(rows), late_count=late)

    if len(rows) < MIN_ROWS or min(late, on_time) < MIN_PER_CLASS:
        result.status = ComparisonStatus.NOT_ENOUGH_HISTORY
        result.message = (
            f"{len(rows)} invoices have a known outcome ({late} late, {on_time} on time). "
            f"A comparison needs {MIN_ROWS}, with at least {MIN_PER_CLASS} of each."
        )
    else:
        older, newest = split_by_issue_date(rows)
        missing = _missing_outcome(older, newest)
        if missing:
            result.status = ComparisonStatus.NOT_ENOUGH_HISTORY
            result.message = (
                f"{len(rows)} invoices have a known outcome ({late} late, {on_time} on time). "
                f"The {len(newest)} newest, issued {_date_range(newest)}, are held out for testing. "
                f"{missing} A comparison needs late and on-time invoices among both the newest and the older ones."
            )
        else:
            _fit(older, newest, result)

    session.add(result)
    session.commit()
    session.refresh(result)
    return result


def split_by_issue_date(rows: list[dict], share: float = TEST_SHARE) -> tuple[list[dict], list[dict]]:
    """Split rows into older ones to train on and the newest ones to test on (issue #148).

    The newest `share` of rows by issue date is held out, rounded up. Rows
    issued at the same moment as the oldest held-out row are held out too, so
    every training row was issued before every held-out row.
    """
    ordered = sorted(rows, key=lambda row: row["issued_at"])
    cut = len(ordered) - math.ceil(len(ordered) * share)
    while 0 < cut < len(ordered) and ordered[cut - 1]["issued_at"] == ordered[cut]["issued_at"]:
        cut -= 1
    return ordered[:cut], ordered[cut:]


def _missing_outcome(older: list[dict], newest: list[dict]) -> str:
    """Say which part lacks late or on-time invoices, or return "" if both parts have both."""
    for name, part in (("newest", newest), ("older", older)):
        late = sum(row["late"] for row in part)
        if late == 0:
            return f"None of the {name} invoices was late."
        if late == len(part):
            return f"All of the {name} invoices were late."
    return ""


def _date_range(rows: list[dict]) -> str:
    """The first and last issue dates of rows already sorted by issue date."""
    return f"{rows[0]['issued_at']:%d %b %Y} to {rows[-1]['issued_at']:%d %b %Y}"


def _fit(older: list[dict], newest: list[dict], result: RiskComparison) -> None:
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, roc_auc_score

    X_train = [[row[name] for name in FEATURES] for row in older]
    y_train = [row["late"] for row in older]
    X_test = [[row[name] for name in FEATURES] for row in newest]
    y_test = [row["late"] for row in newest]
    model = LogisticRegression(max_iter=1000)
    model.fit(X_train, y_train)
    probabilities = model.predict_proba(X_test)[:, 1]
    baseline = [row["rule_score"] for row in newest]

    result.status = ComparisonStatus.OK
    result.test_rows = len(y_test)
    result.logistic_accuracy = round(float(accuracy_score(y_test, [int(p >= 0.5) for p in probabilities])), 3)
    result.logistic_auc = round(float(roc_auc_score(y_test, probabilities)), 3)
    result.baseline_accuracy = round(float(accuracy_score(y_test, [int(s >= HIGH_FROM) for s in baseline])), 3)
    result.baseline_auc = round(float(roc_auc_score(y_test, baseline)), 3)
    result.message = (
        f"Held out the {len(newest)} newest of {len(older) + len(newest)} invoices, "
        f"issued {_date_range(newest)}, and trained on the {len(older)} issued before them. "
        "The rule baseline is still the shipped score."
    )


def latest_comparison(session: Session, user_id: int) -> RiskComparison | None:
    query = owned(select(RiskComparison), RiskComparison, user_id)
    return session.exec(query.order_by(RiskComparison.id.desc()).limit(1)).first()
