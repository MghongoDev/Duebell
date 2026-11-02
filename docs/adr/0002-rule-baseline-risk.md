# 0002 Ship an explainable rule baseline for late-payment risk

## Status
Accepted

## Date
2026-11-03

## Context
The app must turn a client's payment history into a score a small-business owner can act on. There is no real payment dataset in the demo, so a trained model cannot be validated. A score that cannot explain itself is hard to trust and hard to debug. The hackathon also rewards a clear, working idea over an unverifiable model.

## Decision
Ship a rule baseline in `app/risk.py`. It combines prior lateness share, average days late, amount relative to the client's usual amount, and payment terms with fixed weights. It returns a score, a Low/Medium/High level, and short reasons. A client with fewer than two prior invoices gets a limited-data label. A separate script trains logistic regression on synthetic data and reports a held-out comparison beside the baseline.

## Alternatives considered
- **Train a model and ship it.** Rejected because synthetic data proves nothing about real clients, and a black-box score is harder to defend in a demo.
- **Use only a due-date rule.** Rejected because it ignores the client's history, which is the point of the feature.

## Consequences
- Every score comes with reasons a human can check.
- The limited-data label is honest about weak evidence.
- The logistic-regression comparison is a side artifact, not a product claim.
- The weights are hand-tuned and need real data before anyone treats them as calibrated.
