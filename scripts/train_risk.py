"""Compare the rule baseline with logistic regression on synthetic history (issue #13).

The data here is synthetic. This script demonstrates the approach and reports
a held-out comparison. It does not prove the model works on real clients.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.model_selection import train_test_split

FEATURES = ["late_share", "average_days_late", "amount_ratio", "terms_days", "new_client"]


def make_rows(count: int = 800, seed: int = 42) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    for _ in range(count):
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


def main() -> None:
    rows = make_rows()
    X = [[row[name] for name in FEATURES] for row in rows]
    y = [row["late"] for row in rows]

    X_train, X_test, y_train, y_test, rows_train, rows_test = train_test_split(
        X, y, rows, test_size=0.25, random_state=42, stratify=y
    )

    model = LogisticRegression(max_iter=1000)
    model.fit(X_train, y_train)
    probabilities = model.predict_proba(X_test)[:, 1]
    predictions = [1 if value >= 0.5 else 0 for value in probabilities]

    baseline_scores = [rule_score(row) for row in rows_test]
    baseline_predictions = [1 if value >= 0.55 else 0 for value in baseline_scores]

    print("Synthetic data. Not validated on real clients.")
    print(f"Rows: {len(rows)} train: {len(X_train)} test: {len(X_test)}")
    print(f"Rule baseline accuracy: {accuracy_score(y_test, baseline_predictions):.3f}")
    print(f"Rule baseline AUC:      {roc_auc_score(y_test, baseline_scores):.3f}")
    print(f"Logistic accuracy:      {accuracy_score(y_test, predictions):.3f}")
    print(f"Logistic AUC:           {roc_auc_score(y_test, probabilities):.3f}")


if __name__ == "__main__":
    main()
