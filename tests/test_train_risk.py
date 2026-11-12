"""The offline risk comparison holds out the newest synthetic invoices (issue #148)."""

from scripts.train_risk import compare, make_rows


def test_synthetic_rows_have_issue_dates():
    rows = make_rows(count=40)
    assert len({row["issued_at"] for row in rows}) == 40


def test_offline_comparison_tests_on_the_newest_quarter():
    rows = make_rows()
    result = compare(rows)
    assert result["test_rows"] == 200
    assert result["train_rows"] == 600
    assert result["train_last"] < result["test_first"]
    newest = sorted(row["issued_at"] for row in rows)[-200:]
    assert (result["test_first"], result["test_last"]) == (newest[0], newest[-1])
    for name in ("baseline_accuracy", "baseline_auc", "logistic_accuracy", "logistic_auc"):
        assert 0.0 <= result[name] <= 1.0
