# Architecture

Duebell is one FastAPI process over one SQLite file ([ADR 0005](../adr/0005-sqlite-and-payment-sync.md)). The browser gets server-rendered pages and a little JavaScript. The app reaches PayPal through one [gateway](../../GLOSSARY.md) and the model through one parser function. Neither the browser nor the model writes to the database directly.

## Components

| Path | Role |
| --- | --- |
| `app/web.py` | App wiring, the sign-in middleware, security headers. |
| `app/routes/` | The routes, one module per area: sign-in, dashboard, invoices, risk, schedules, reminders, the PayPal webhook. `common.py` holds the session dependency and the signed-in user helper. |
| `app/auth.py` | Password hashing, the signed session cookie, the sign-in lockout. |
| `app/ownership.py` | Scopes every query to one user, or to every user for the scheduler. |
| `app/config.py` | Reads the [configuration](../reference/configuration.md). |
| `app/models.py` | Tables, and the start-up step that adds new columns and indexes. |
| `app/validation.py` | `InvoiceProposal` and the rules the form and the AI parser share. |
| `app/services.py` | Creating and sending invoices, dashboard rows and totals, payment sync. |
| `app/risk.py` | The rule baseline, with reasons. |
| `app/learning.py` | Refits the risk comparison on a user's own invoices. |
| `app/reminders.py` | Drafting, editing, approving and rejecting reminders, and the send channels. |
| `app/schedules.py` | Repeats, instalment plans and scheduled invoices. |
| `app/paypal.py` | The gateway protocol and its mock, sandbox and toolkit implementations. |
| `app/mailer.py` | The email protocol and its mock and SMTP implementations. |
| `app/llm.py` | The model call, JSON checks and the demo parser. |
| `app/retry.py` | Bounded retries with backoff for outbound calls. |
| `app/jobs.py` | The background scheduler. |
| `app/templates/`, `app/static/` | Pages, the AG Grid setup, styles. |
| `scripts/` | Seeding, users, checks and webhook registration. See [commands](../reference/commands.md). |

## A request

1. The sign-in middleware reads the session cookie. Without a valid one, it turns the request away, unless the path is public.
2. The route runs as that user. Every read passes the user's ID as its scope, and a lookup by ID returns nothing for another user's record.
3. Writes go through `services`, `reminders` or `schedules`. Nothing that reaches PayPal or a client runs without an approval route.

Every route is a plain `def`, so FastAPI runs it in its threadpool, and the middleware's user lookup runs there too. A slow model or PayPal call holds up only its own request, never the health check or another user. The webhook reads its body in an async dependency, then runs like any other route. Because routes run in parallel, anything that sends claims its row first with one conditional update, and only one request wins.

## Creating an invoice

1. The user fills the form, or `POST /parse` turns free text into the same six fields.
2. Duebell rejects any extra key from the model. `InvoiceProposal` and `validate_proposal` check every value, the same way for both paths ([ADR 0004](../adr/0004-llm-as-form-filler.md)).
3. `create_invoice` finds or creates the client, scores the risk, and saves a `DRAFT`.
4. The gateway creates the PayPal invoice, then sends it. If the send fails, the invoice stays a draft with its PayPal ID, and a retry sends that draft.

## Background work

The scheduler runs inside the app process.

- Every 30 minutes it drafts reminders and moves due scheduled invoices to pending, for every user. It never sends.
- Every six hours it refits the risk comparison once per user.

The scheduler is the only caller that may scan every user. It says so explicitly, with the `EVERY_USER` scope.

## Trust boundaries

| Boundary | Rule |
| --- | --- |
| Browser to app | A session for every non-public path. All input validated: amounts capped, currencies on a list, text lengths limited. |
| User to user | Every query scoped to one user. |
| Model to app | An untrusted form filler. Duebell checks its JSON against a fixed set of keys, then validates the values. |
| PayPal to app | PayPal checks the webhook signature before Duebell changes anything. Event IDs make replays harmless, and a status only moves forward. |
| App to PayPal | Only through the gateway, and only in `sandbox` or `toolkit` mode. |
| App to client | Only after an approval, through PayPal or email. |

## Further reading

- [Approvals and sending](approvals-and-sending.md): why nothing goes out without a click, and how retries avoid sending twice.
- [Risk scores](risk-scores.md): the baseline, the refit, and currencies.
- [Security](security.md): sign-in, ownership and limits.
- [Decision records](../adr/README.md).
