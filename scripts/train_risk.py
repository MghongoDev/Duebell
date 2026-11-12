"""Compare the rule baseline with logistic regression on synthetic history (issues #13, #148).

The data here is synthetic. This script demonstrates the approach and reports
a held-out comparison. It does not prove the model works on real clients.
Like the dashboard refit, it tests on the newest quarter of invoices by issue
date and trains on the older ones.
"""

from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, roc_auc_score

from app.learning import split_by_issue_date

FEATURES = ["late_share", "average_days_late", "amount_ratio", "terms_days", "new_client"]
FIRST_ISSUED = datetime(2024, 1, 1, tzinfo=timezone.utc)


def make_rows(count: int = 800, seed: int = 42) -> list[dict]:
    """Synthetic rows, one invoice issued per day from FIRST_ISSUED on."""
    rng = random.Random(seed)
    rows = []
    for index in range(count):
        prior_count = rng.choice([0, 1, 2, 4, 6])
        late_share = 0.0 if prior_count == 0 else rng.random()
        average_days_late = 0.0 if prior_count == 0 else rng.uniform(0, 25) * late_share
        amount_ratio = rng.uniform(0.5, 2.5)
        terms_days = rng.choice([14, 30, 45, 60])
        probability = (
            0.10
            + 0.50 * late_share
            + 0.020 * average_days_late
            + 0.05 * max(0.0, amount_ratio - 1.0)
            + (0.05 if terms_days > 30 else 0.0)
        )
        probability = min(0.95, probability)
        late = 1 if rng.random() < probability else 0
        rows.append(
            {
                "late_share": late_share,
                "average_days_late": average_days_late,
                "amount_ratio": amount_ratio,
                "terms_days": terms_days,
                "new_client": 1 if prior_count == 0 else 0,
                "late": late,
                "issued_at": FIRST_ISSUED + timedelta(days=index),
            }
        )
    return rows


def rule_score(row: dict) -> float:
    if row["new_client"]:
        return 0.40
    score = 0.18 + 0.45 * row["late_share"]
    score += 0.35 * min(row["average_days_late"] / 30.0, 1.0)
    if row["amount_ratio"] >= 1.5:
        score += 0.08
    if row["terms_days"] > 30:
        score += 0.04
    return max(0.0, min(1.0, score))


def compare(rows: list[dict]) -> dict:
    """Train on the older rows, test both models on the newest quarter (issue #148)."""
    older, newest = split_by_issue_date(rows)
    X_train = [[row[name] for name in FEATURES] for row in older]
    y_train = [row["late"] for row in older]
    X_test = [[row[name] for name in FEATURES] for row in newest]
    y_test = [row["late"] for row in newest]

    model = LogisticRegression(max_iter=1000)
    model.fit(X_train, y_train)
    probabilities = model.predict_proba(X_test)[:, 1]
    predictions = [1 if value >= 0.5 else 0 for value in probabilities]

    baseline_scores = [rule_score(row) for row in newest]
    baseline_predictions = [1 if value >= 0.55 else 0 for value in baseline_scores]

    return {
        "train_rows": len(older),
        "test_rows": len(newest),
        "train_last": older[-1]["issued_at"],
        "test_first": newest[0]["issued_at"],
        "test_last": newest[-1]["issued_at"],
        "baseline_accuracy": accuracy_score(y_test, baseline_predictions),
        "baseline_auc": roc_auc_score(y_test, baseline_scores),
        "logistic_accuracy": accuracy_score(y_test, predictions),
        "logistic_auc": roc_auc_score(y_test, probabilities),
    }


def main() -> None:
    rows = make_rows()
    result = compare(rows)

    print("Synthetic data. Not validated on real clients.")
    print(f"Rows: {len(rows)} train: {result['train_rows']} test: {result['test_rows']}")
    print(
        f"Held out the newest, issued {result['test_first']:%d %b %Y} to {result['test_last']:%d %b %Y}; "
        f"trained on those issued up to {result['train_last']:%d %b %Y}."
    )
    print(f"Rule baseline accuracy: {result['baseline_accuracy']:.3f}")
    print(f"Rule baseline AUC:      {result['baseline_auc']:.3f}")
    print(f"Logistic accuracy:      {result['logistic_accuracy']:.3f}")
    print(f"Logistic AUC:           {result['logistic_auc']:.3f}")


if __name__ == "__main__":
    main()
