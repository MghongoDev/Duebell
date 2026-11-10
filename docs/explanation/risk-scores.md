# Risk scores

Every invoice gets a **risk score** from 0 to 1, a **risk level**, and reasons a person can check. The score comes from a fixed rule, the **rule baseline** ([ADR 0002](../adr/0002-rule-baseline-risk.md)). A trained model runs beside it as a comparison and never replaces it. Terms in bold are in the [glossary](../../GLOSSARY.md).

## Which invoices count

The rule reads only the client's earlier invoices with a **known outcome**:

- paid, or
- unpaid and at least one full day past its due date. This one counts as late.

An open invoice that is not yet due says nothing about how the client pays, so it waits until it is paid or overdue. Drafts were never sent and cancelled invoices were withdrawn, so neither ever counts. The live score and the risk comparison build history with the same code, `history_as_of` in `app/services.py`, and the same rule, `outcome_known` in `app/risk.py`.

## The rule baseline

| Input | Effect |
| --- | --- |
| No earlier invoices with a known outcome | Score 0.40. The reason says "New client", or, if the client has open invoices, how many are not yet due. |
| Share of earlier invoices paid late | Adds up to 0.45. |
| Average days late, capped at 30 | Adds up to 0.35. |
| This invoice is at least 1.5 times the client's usual amount | Adds 0.08. |
| Terms longer than 30 days | Adds 0.04. |

A client with history starts from 0.18. The level is Low below 0.30, High from 0.55, and Medium between.

A client who paid four invoices about 25 days late scores 0.92, High. Four more open invoices that are not yet due leave the score at 0.92.

The weights are set by hand. Real payment data has not tested them.

## Limited data

A client with fewer than two earlier invoices with a known outcome gets a **limited-data score**. A client with only drafts and open invoices is one of these. So is a client's first invoice in a new currency, because there is no usual amount in that currency to compare with.

One of the reasons always says why the data is limited. The dashboard shows that reason under the "Limited data" label.

## Currencies

Lateness is behaviour, so it uses the client's whole history. Amounts compare only within one currency. A client's EUR history never changes the usual amount for a USD invoice. Dashboard totals show one figure per currency and never add currencies together.

## The risk comparison

The **risk comparison** asks whether a trained model would do better than the rule on this app's own data. It refits every six hours, and when the user clicks `Refit now`.

1. It takes each of the user's invoices whose outcome is known now, by the rule above.
2. For each, it rebuilds the inputs above as they stood on the day the invoice was issued, from the client's earlier invoices only. A payment that came later never leaks into the row, and an earlier invoice counts only if its outcome was known on that day.
3. It holds out a quarter of the rows at random, fits logistic regression on the rest, and scores both models on the held-out rows.

Below 30 invoices, or with fewer than 5 late or 5 on time, the dashboard says there is not enough history and shows no number. The seeded demo sits below that line on purpose. Each user's refit uses only that user's invoices.

The held-out rows are a random sample, not the most recent invoices. A split by time would better show how the model would do on next month's invoices.

### Known gap: cancellations

An invoice does not record when it was cancelled. So the rebuild leaves out every invoice that is cancelled now, even one cancelled after the day being rebuilt. On that day it was still open, or already overdue. A row can therefore miss an overdue invoice the client had at the time. Fixing this needs a cancellation date on the invoice.

## Current figures

Measured on 10 November 2026, after the known-outcome rule (issue #46). The rule did not change any of them.

| Run | Result |
| --- | --- |
| `scripts/evals.py` | Reminders 10/10, webhook replay pass, overall pass. Parsing cases skipped offline. |
| `scripts/train_risk.py` | 800 synthetic rows, 200 held out. Rule baseline accuracy 0.705, AUC 0.782. Logistic regression accuracy 0.705, AUC 0.816. |
| Refit on the seeded demo | 22 invoices with a known outcome, 18 late and 4 on time. Below the line, so no number. |

`scripts/train_risk.py` runs the comparison on synthetic histories. It builds feature rows directly and does not read invoices, so the known-outcome rule does not touch it. Its result shows the method works, not that the model suits real clients.
