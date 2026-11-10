# HTTP routes

Every route needs a signed-in user except the four public paths: `/login`, `/healthz`, `/static/...` and `/webhooks/paypal` ([ADR 0008](../adr/0008-sign-in-and-ownership.md)). Without a session, a `GET` page redirects to `/login` and anything else returns `401` with `{"ok": false, "error": "Sign in first."}`.

Each route acts only on the signed-in user's records. Another user's record gets the same `404` as a missing one.

Request bodies are HTML form fields, not JSON. JSON responses carry `"ok": true` or `"ok": false` with an `"error"` message. FastAPI's generated schema is at `/docs` once you sign in.

## Pages and sign-in

| Method | Path | Does |
| --- | --- | --- |
| `GET` | `/healthz` | Returns `{"ok": true, "service": "duebell"}`. It does not query the database or PayPal. |
| `GET` | `/login` | The sign-in page. |
| `POST` | `/login` | Fields `email`, `password`. Sets the session cookie and redirects to `/`. A wrong password, or a locked email, returns `401` with the page. An `Origin` header naming another host returns `403`. |
| `POST` | `/logout` | Clears the cookie and redirects to `/login`. |
| `GET` | `/` | The dashboard. |

## Invoices

| Method | Path | Does |
| --- | --- | --- |
| `POST` | `/invoices` | Creates and sends an invoice from the form. Fields: `client_name`, `client_email`, `description`, `amount`, `currency`, `terms_days`. Optional: `repeat` (`WEEK` or `MONTH`) with `repeat_cycles`, or `instalments` (2 to 6) with `instalment_interval`. Redirects to `/` on success. Returns the page with field errors and `422` on bad input, or `502` when PayPal fails. |
| `POST` | `/parse` | Field `description`, the free text. Returns `{"proposal": {...}, "missing": [...]}` with the six form fields. A field the model left out is an empty string and is named in `missing`. Creates nothing. `422` if the answer has an unknown key or a wrongly typed value, or a filled field fails validation. |
| `GET` | `/api/invoices` | The user's invoices as dashboard rows. |
| `POST` | `/api/invoices/{invoice_id}/refresh` | Reads the status from PayPal and stores it if it moves the invoice forward (see [statuses](data-model.md#invoice)). Returns `{"status": ...}` with the stored status. |
| `POST` | `/api/invoices/{invoice_id}/simulate-payment` | Records a payment through the gateway and marks the invoice `PAID`. `409` if the invoice has no PayPal ID or is already paid, cancelled or refunded. |

## Risk

| Method | Path | Does |
| --- | --- | --- |
| `POST` | `/api/risk/preview` | Fields `client_email`, `amount`, `terms_days`, `currency`. Scores an invoice that does not exist yet. Returns `level`, `score`, `reasons`. Writes nothing. |
| `POST` | `/api/risk/refit` | Refits the [risk comparison](../explanation/risk-scores.md) on the user's invoices and returns it. |
| `GET` | `/api/risk/comparison` | The latest comparison, or `null`. |

## Reminders

| Method | Path | Does |
| --- | --- | --- |
| `POST` | `/api/reminders/scan` | Drafts reminders and moves due scheduled invoices to pending. Sends nothing. Returns `{"created": n, "scheduled": m}`. |
| `GET` | `/api/reminders` | The user's reminders, newest first. |
| `POST` | `/api/reminders/{reminder_id}/edit` | Field `draft_text`. Saves a new draft. `409` unless the draft is pending. |
| `POST` | `/api/reminders/{reminder_id}/approve` | Field `channel`: `paypal` (the default), `email` or `both`. Sends through each channel not yet used. Returns `status`, `sent_channels`, and a `warning` if PayPal did not confirm in time. `409` if the draft is not pending or the invoice has no PayPal ID. `409` too if the invoice is paid, cancelled or refunded; the draft is then marked `REJECTED`. `502` if a channel fails; the draft stays pending. |
| `POST` | `/api/reminders/{reminder_id}/reject` | Marks the draft `REJECTED`. A later scan may draft a new one. |

## Scheduled invoices

| Method | Path | Does |
| --- | --- | --- |
| `POST` | `/api/scheduled/{item_id}/approve` | Creates and sends a pending scheduled invoice. If an earlier try created the PayPal draft but failed to send it, sends that draft instead of creating another; if PayPal has since sent it, marks the item sent. Returns `status`, `invoice_id`, and a `warning` if PayPal did not confirm the send in time. `409` if the item is not pending or another approval holds it. `502` if PayPal fails; the item stays pending. |
| `POST` | `/api/scheduled/{item_id}/reject` | Skips this invoice. A repeat carries on. |
| `POST` | `/api/schedules/{schedule_id}/cancel` | Stops the schedule and cancels its upcoming and pending invoices. Sent invoices stay as they are. |

## PayPal webhook

| Method | Path | Does |
| --- | --- | --- |
| `POST` | `/webhooks/paypal` | Public. Checks the signature through PayPal before anything else. Stores each event once by its PayPal event ID, so a replay changes nothing. `400` for a bad signature, a body that is not UTF-8, invalid JSON or a missing event ID. `413` above 1 MB. |

The webhook maps these PayPal events to invoice statuses: `INVOICING.INVOICE.SENT`, `.PAID`, `.CANCELLED` and `.REFUNDED`. The event belongs to the user who owns the invoice. Duebell still stores an event that would move the invoice backward, such as `SENT` after `PAID`, but the event changes nothing and returns `"applied": false`.
