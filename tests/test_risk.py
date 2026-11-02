"""Pure seam tests for the risk baseline (issue #12)."""

from datetime import datetime, timedelta, timezone

from app.risk import HistoryInvoice, score_invoice

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def history(amount_cents, days_late, count):
    return [
        HistoryInvoice(
            amount_cents=amount_cents,
            due_at=NOW - timedelta(days=30),
            paid_at=NOW - timedelta(days=30) + timedelta(days=days_late),
        )
        for _ in range(count)
    ]


def test_new_client_gets_limited_data_score():
    result = score_invoice(120000, 14, [], now=NOW)
    assert result.limited_data is True
    assert result.level == "MEDIUM"
    assert any("New client" in reason for reason in result.reasons)


def test_habitual_late_payer_scores_high():
    result = score_invoice(100000, 14, history(100000, 9, 4), now=NOW)
    assert result.level == "HIGH"
    assert any("Paid 4 of last 4 invoices late" in reason for reason in result.reasons)


def test_three_of_four_late_scores_high():
    late = history(100000, 9, 3)
    on_time = history(100000, 0, 1)
    result = score_invoice(100000, 14, late + on_time, now=NOW)
    assert result.level == "HIGH"
    assert any("Paid 3 of last 4 invoices late" in reason for reason in result.reasons)


def test_reliable_payer_scores_low():
    result = score_invoice(100000, 14, history(100000, 0, 4), now=NOW)
    assert result.level == "LOW"
    assert result.reasons[0].startswith("Paid all 4")


def test_unpaid_past_due_counts_as_late():
    overdue = [HistoryInvoice(amount_cents=100000, due_at=NOW - timedelta(days=10), paid_at=None)]
    result = score_invoice(100000, 14, overdue, now=NOW)
    assert result.level == "HIGH"
    assert result.limited_data is True


def test_large_amount_and_long_terms_raise_the_score():
    reliable = history(100000, 0, 4)
    base = score_invoice(100000, 14, reliable, usual_amount_cents=100000, now=NOW).score
    raised = score_invoice(200000, 60, reliable, usual_amount_cents=100000, now=NOW).score
    assert raised > base
