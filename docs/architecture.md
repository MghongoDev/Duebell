# Architecture

Duebell is one FastAPI process with a SQLite file. The browser gets server-rendered Jinja pages and a small amount of JavaScript. PayPal is reached through one gateway interface. The model is reached through one parser function. Neither the browser nor the model can write to the database directly.

## Components

| Path | Role |
| --- | --- |
| `app/config.py` | Reads environment settings and runtime modes. |
| `app/models.py` | `Client`, `Invoice`, `Reminder`, and `Event` tables. |
| `app/validation.py` | `InvoiceProposal` and the rules shared by the form and the AI parser. |
| `app/services.py` | Invoice creation, dashboard rows, status changes, webhooks. |
| `app/risk.py` | Rule baseline for late-payment risk, with reasons. |
| `app/reminders.py` | Draft text, eligibility, editing, approval, rejection. |
| `app/llm.py` | Anthropic-compatible call, JSON extraction, field allowlist, demo parser. |
| `app/paypal.py` | Mock and sandbox gateways. |
| `app/retry.py` | Bounded retry with backoff for outbound calls. |
| `app/jobs.py` | APScheduler job that drafts reminders. |
| `app/web.py` | Routes, templates, security headers, app wiring. |
| `app/templates/` | Server-rendered pages and the AG Grid setup. |
| `scripts/` | Seed data, smoke runs, risk comparison, evals, webhook registration. |

## Create an invoice

1. The form posts fields, or `POST /parse` sends free text to the parser.
2. The parser returns JSON. Extra keys are rejected, then `InvoiceProposal` and `validate_proposal` check the values.
3. `create_invoice` finds or creates the client, computes the risk, and writes a `DRAFT` invoice.
4. The gateway creates the PayPal invoice, then sends it. The invoice becomes `SENT`.
5. The dashboard reads the invoice rows and totals.

The AI parser and the manual form share the validation and creation path. That is the guardrail from ADR 0004.

## Risk

`client_history` turns the client's prior invoices into `HistoryInvoice` records. `score_invoice` extracts lateness share, average days late, amount ratio, and terms, then returns a score, a level, and reasons. A client with fewer than two prior invoices gets a limited-data label. The shipped path uses the rule baseline from ADR 0002. The logistic-regression script is a side comparison, not a runtime dependency.

## Reminders

`draft_reminders` looks for invoices that are `SENT` and either overdue or due soon with medium or high risk. It skips invoices that already have a pending or sent reminder. It writes `PENDING` drafts with text built only from the invoice and client facts. A human can edit or reject a draft. Only an approval calls `send_reminder`, and the edited text is what PayPal receives. The scheduler runs the same draft pass every 30 minutes. See ADR 0003.

## Payment sync

PayPal posts to `/webhooks/paypal`. The gateway verifies the signature through PayPal. `handle_webhook` stores the event by its PayPal id, so a replay is a no-op. A paid event marks the invoice paid and sets `paid_at`. A refresh action reads the current status from PayPal for cases where a webhook does not arrive. The mock gateway offers a simulate-payment action for the offline demo. See ADR 0005.

## Outbound calls

`app/retry.py` retries timeouts, transport errors, HTTP 429, and HTTP 5xx up to three times with exponential backoff. The sandbox gateway and the live parser use it. Exhausted retries become a `PayPalError` or an `LLMError` that the route renders as a readable message.

## Trust boundaries

- **Browser to app:** All input is validated. Amounts are capped, currencies are allowlisted, and text fields have length limits.
- **Model to app:** The model is an untrusted form filler. Its JSON is allowlisted and validated.
- **PayPal to app:** The webhook signature is checked before state changes. Event ids make replay safe.
- **App to PayPal:** Only the sandbox adapter makes network calls, and only when `PAYPAL_MODE=sandbox`.
- **App to model:** Only the parser makes a model call, and only when `LLM_MODE=live`.

## Runtime modes

| Mode | PayPal | AI parsing |
| --- | --- | --- |
| Default | In-memory mock | Narrow demo parser; manual form works |
| `PAYPAL_MODE=sandbox` | PayPal sandbox REST | Off |
| `LLM_MODE=live` | In-memory mock | Anthropic-compatible endpoint |
| Both | PayPal sandbox REST | Anthropic-compatible endpoint |

## Tests and evals

`pytest` covers validation, invoice creation, the PayPal gateway boundary, retries, payment sync, risk, AI guardrails, reminders, and security limits. `scripts/evals.py` checks parse accuracy against a live model, reminder text against fixed facts, and webhook replay. The parse eval is skipped when the model is off. `scripts/smoke.py` runs three clean reset-to-demo cycles.

## Deliberate limits

- One user, no login. The app is a local demo.
- SQLite and one process. No queue, no multiple instances.
- The risk weights are hand-tuned and not calibrated on real payments.
- The demo moves no real money.
