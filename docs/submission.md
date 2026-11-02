# Submission notes

## Elevator pitch

Duebell is an invoice-to-cash agent for freelancers. Describe the job in plain English, and it creates and sends a PayPal invoice, scores how likely the client is to pay late, and drafts a reminder in the right tone. You approve the reminder before PayPal sends it.

## The problem

A freelancer finishes the work, sends the invoice, and then has to remember to chase it. The chase is awkward, easy to postpone, and easy to get wrong. Most tools stop at sending the invoice. The follow-up is still manual.

## What it does

1. Turns a sentence into invoice fields. The model fills a form, and the same validation as the manual form checks the result.
2. Creates a PayPal invoice and sends it.
3. Scores late-payment risk from the client's history, with reasons a person can check.
4. Scans for invoices that are overdue or due soon and drafts a reminder. A human approves it. Only then does PayPal send it.
5. Handles a paid webhook, a manual refresh, and a mock payment. The next invoice sees the new history.

## How it uses PayPal

- PayPal Invoicing v2 creates and sends invoices.
- PayPal's reminder endpoint sends an approved reminder.
- PayPal webhooks report paid, cancelled, and refunded events.
- Webhook signatures are verified before any state changes. Event ids make replays safe.
- The app runs against the PayPal sandbox. It moves no real money.

## How it uses AI

- A model parses a short description into a fixed JSON shape. Extra keys are rejected. The result passes the same validation as the manual form, so the model cannot create an invoice on its own.
- The prompt treats the user text as data, not instructions. A test checks that injected text stays in a field.
- The risk score is an explainable rule baseline, not an opaque model. A separate script trains logistic regression on synthetic data and reports the comparison beside the baseline. The app does not claim the synthetic result proves real-world accuracy.

## Evidence

- `pytest`: 66 tests pass.
- Live evals on 3 November 2026: parse 10 of 10 cases exact with 50 of 50 fields correct, reminders 10 of 10, webhook pass.
- `scripts/smoke.py`: three clean reset-to-demo cycles, each with six clients, 26 invoices, two pending drafts, and zero sends.
- The dashboard screenshot in the README comes from the seeded mock mode.

## Built with

Python 3.12+, FastAPI, Jinja2, HTMX, AG Grid Community, SQLModel, SQLite, APScheduler, httpx, scikit-learn for the side comparison, and pytest.

## Challenges

- PayPal sandbox Invoicing calls returned `403 NOT_AUTHORIZED` during development even after the Invoicing feature was enabled. The mock gateway keeps the demo reproducible. The sandbox adapter is wired, and its request payloads are tested without a network call.
- The model can return a plausible wrong value. The fix was to make the model a form filler, not an agent, and to keep the manual form as the fallback.
- Reminder tone is a judgement call. The app uses a small, visible rule and shows the whole draft before approval.

## What is next

- Calibrate risk on real, consented payment history.
- Learn from operator edits to improve the draft tone.
- Add partial payments and multi-currency support.
- Add authentication before anyone hosts this beyond a local demo.

## Deliberate limits

Duebell is a single-user sandbox demo with no login. Do not expose it on the public internet. The risk weights are hand-tuned. The demo moves no real money.

## Links

- Repository: https://github.com/MghongoDev/duebell
- Video: add the public video link before submitting.
