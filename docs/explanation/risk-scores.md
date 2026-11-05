# Risk scores

Every invoice gets a **risk score** from 0 to 1, a **risk level**, and reasons a person can check. The score comes from a fixed rule, the **rule baseline** ([ADR 0002](../adr/0002-rule-baseline-risk.md)). A trained model runs beside it as a comparison and never replaces it. Terms in bold are in the [glossary](../../GLOSSARY.md).

## The rule baseline

The rule reads the client's earlier invoices.

| Input | Effect |
| --- | --- |
| No earlier invoices | Score 0.40, with the reason "New client". |
| Share of earlier invoices paid late | Adds up to 0.45. |
| Average days late, capped at 30 | Adds up to 0.35. |
| This invoice is at least 1.5 times the client's usual amount | Adds 0.08. |
| Terms longer than 30 days | Adds 0.04. |

A client with history starts from 0.18. An unpaid invoice past its due date counts as late. The level is Low below 0.30, High from 0.55, and Medium between.

The weights are set by hand. Real payment data has not tested them.

## Limited data

A client with fewer than two earlier invoices gets a **limited-data score**. So does a client's first invoice in a new currency, because there is no usual amount in that currency to compare with. The reasons say which applies.

## Currencies

Lateness is behaviour, so it uses the client's whole history. Amounts compare only within one currency. A client's EUR history never changes the usual amount for a USD invoice. Dashboard totals show one figure per currency and never add currencies together.

## The risk comparison

The **risk comparison** asks whether a trained model would do better than the rule on this app's own data. It refits every six hours, and when the user clicks `Refit now`.

1. It takes each of the user's invoices whose outcome is known: paid, marked as paid, or unpaid past its due date.
2. For each, it rebuilds the inputs above as they stood on the day the invoice was issued, from the client's earlier invoices only. A payment that came later never leaks into the row.
3. It holds out a quarter of the rows at random, fits logistic regression on the rest, and scores both models on the held-out rows.

Below 30 invoices, or with fewer than 5 late or 5 on time, the dashboard says there is not enough history and shows no number. The seeded demo sits below that line on purpose. Each user's refit uses only that user's invoices.

The held-out rows are a random sample, not the most recent invoices. A split by time would better show how the model would do on next month's invoices.

`scripts/train_risk.py` runs the same comparison on synthetic histories. Its result shows the method works, not that the model suits real clients.
