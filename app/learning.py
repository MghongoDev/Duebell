"""Refit the risk comparison on the app's own payment history (issue #22).

The rule baseline stays the shipped score (ADR 0002). This module fits a
logistic regression on invoices whose outcome is known and stores a held-out
comparison beside the baseline, so the operator can see whether real history
says anything the hand-tuned weights miss.

Each invoice becomes one row. Its features are computed as of its issue date
from the client's earlier invoices only, so the model never sees a payment
that had not happened yet.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, select

from app.models import Invoice, RiskComparison, utcnow
from app.risk import HIGH_FROM, HistoryInvoice, extract_features, score_invoice

FEATURES = ["late_share", "average_days_late", "amount_ratio", "terms_days", "new_client"]
MIN_ROWS = 30
MIN_PER_CLASS = 5
TEST_SHARE = 0.25
SETTLED = {"PAID"}
SKIPPED = {"DRAFT", "CANCELLED"}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def outcome(invoice: Invoice, now: datetime) -> int | None:
    """1 if paid late or unpaid past due, 0 if paid on time, None if unknown."""
    if invoice.status in SKIPPED:
        return None
    due = _aware(invoice.due_at)
    paid = _aware(invoice.paid_at)
    if invoice.status in SETTLED and paid is not None:
        return 1 if (paid - due).days > 0 else 0
    if paid is None and due < now:
        return 1
    return None


def _history_as_of(invoices: list[Invoice], as_of: datetime) -> list[HistoryInvoice]:
    history = []
    for item in invoices:
        if item.status in SKIPPED or _aware(item.issued_at) >= as_of:
            continue
        paid = _aware(item.paid_at)
        history.append(
            HistoryInvoice(
                amount_cents=item.amount_cents,
                due_at=_aware(item.due_at),
                paid_at=paid if paid is not None and paid <= as_of else None,
                currency=item.currency,
            )
        )
    return history


def training_rows(session: Session, now: datetime | None = None, user_id: int | None = None) -> list[dict]:
    now = _aware(now) or utcnow()
    query = select(Invoice) if user_id is None else select(Invoice).where(Invoice.user_id == user_id)
    invoices = list(session.exec(query).all())
    by_client: dict[int, list[Invoice]] = {}
    for invoice in invoices:
        by_client.setdefault(invoice.client_id, []).append(invoice)

    rows = []
    for invoice in invoices:
        label = outcome(invoice, now)
        if label is None:
            continue
        as_of = _aware(invoice.issued_at)
        history = _history_as_of(by_client[invoice.client_id], as_of)
        features = extract_features(
            invoice.amount_cents, invoice.terms_days, history, now=as_of, currency=invoice.currency
        )
        rule = score_invoice(
            invoice.amount_cents, invoice.terms_days, history, now=as_of, currency=invoice.currency
        )
        row = {name: float(features[name]) for name in FEATURES}
        row["rule_score"] = rule.score
        row["late"] = label
        rows.append(row)
    return rows


def refit(session: Session, now: datetime | None = None, user_id: int | None = None) -> RiskComparison:
    """Refit on one user's invoices. Users never train on each other's clients."""
    rows = training_rows(session, now, user_id)
    late = sum(row["late"] for row in rows)
    on_time = len(rows) - late
    result = RiskComparison(user_id=user_id, rows_used=len(rows), late_count=late)

    if len(rows) < MIN_ROWS or min(late, on_time) < MIN_PER_CLASS:
        result.status = "NOT_ENOUGH_HISTORY"
        result.message = (
            f"{len(rows)} invoices have a known outcome ({late} late, {on_time} on time). "
            f"A comparison needs {MIN_ROWS}, with at least {MIN_PER_CLASS} of each."
        )
    else:
        _fit(rows, result)

    session.add(result)
    session.commit()
    session.refresh(result)
    return result


def _fit(rows: list[dict], result: RiskComparison) -> None:
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import accuracy_score, roc_auc_score
    from sklearn.model_selection import train_test_split

    X = [[row[name] for name in FEATURES] for row in rows]
    y = [row["late"] for row in rows]
    X_train, X_test, y_train, y_test, _, rows_test = train_test_split(
        X, y, rows, test_size=TEST_SHARE, random_state=42, stratify=y
    )
    model = LogisticRegression(max_iter=1000)
    model.fit(X_train, y_train)
    probabilities = model.predict_proba(X_test)[:, 1]
    baseline = [row["rule_score"] for row in rows_test]

    result.status = "OK"
    result.test_rows = len(y_test)
    result.logistic_accuracy = round(float(accuracy_score(y_test, [int(p >= 0.5) for p in probabilities])), 3)
    result.logistic_auc = round(float(roc_auc_score(y_test, probabilities)), 3)
    result.baseline_accuracy = round(float(accuracy_score(y_test, [int(s >= HIGH_FROM) for s in baseline])), 3)
    result.baseline_auc = round(float(roc_auc_score(y_test, baseline)), 3)
    result.message = (
        f"Held out {len(y_test)} of {len(rows)} invoices. The rule baseline is still the shipped score."
    )


def latest_comparison(session: Session, user_id: int | None = None) -> RiskComparison | None:
    query = select(RiskComparison)
    if user_id is not None:
        query = query.where(RiskComparison.user_id == user_id)
    return session.exec(query.order_by(RiskComparison.id.desc()).limit(1)).first()
