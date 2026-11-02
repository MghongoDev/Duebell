# 0005 Use SQLite, a signed webhook, and a manual refresh for payment sync

## Status
Accepted

## Date
2026-11-03

## Context
The demo needs one source of truth for invoices and events, a way to learn about payments, and a way to recover when a webhook does not arrive. It runs as a single process for one user. A queue, a worker, and a hosted database would add setup cost without proving anything about the product.

## Decision
Use SQLModel over SQLite, created on startup. Accept PayPal webhooks at `/webhooks/paypal`, verify the signature through PayPal, and store each event by its PayPal event id. On a paid event, mark the invoice paid. Also provide a `Refresh from PayPal` action that reads the invoice status and a `Simulate payment` action for the mock demo. Run the reminder scan in an APScheduler background job and expose `Scan now`.

## Alternatives considered
- **PostgreSQL and a task queue.** Rejected because the demo has one user and no concurrency pressure.
- **Webhook only, with no polling.** Rejected because webhooks fail, and a demo should recover without a database console.
- **Polling only.** Rejected because webhooks are the real integration and the signature and idempotency paths deserve to be shown.

## Consequences
- The database is one file and the whole app is one process.
- The same PayPal event applied twice changes nothing.
- A webhook that never arrives can be recovered with a refresh.
- SQLite is not suitable for multiple app instances. That is a deliberate demo limit.
