# HTTP routes

Every route needs a signed-in user except the five public paths: `/login`, `/logout`, `/healthz`, `/static/...` and `/webhooks/paypal` ([ADR 0008](../adr/0008-sign-in-and-ownership.md)). Without a valid session, as after the 12-hour expiry, a page or form post redirects to `/login` with `303`. Routes under `/api/` and `POST /parse` return `401` with `{"ok": false, "error": "Sign in first."}` instead.

Each route acts only on the signed-in user's records. Another user's record gets the same `404` as a missing one.

Request bodies are HTML form fields, not JSON. JSON responses carry `"ok": true` or `"ok": false` with an `"error"` message. FastAPI's generated schema is at `/docs` once you sign in.

## Pages and sign-in

| Method | Path | Does |
| --- | --- | --- |
| `GET` | `/healthz` | Returns `{"ok": true, "service": "duebell"}`. It does not query the database or PayPal. |
| `GET` | `/login` | The sign-in page. |
| `POST` | `/login` | Fields `email`, `password`. Sets the session cookie and redirects to `/`. A wrong password, a locked email, an email over 254 characters or without `@`, or a password over 1,024 characters returns `401` with the page. An `Origin` header naming another host returns `403`. |
| `POST` | `/logout` | Clears the cookie and redirects to `/login`, with or without a valid session. |
| `GET` | `/` | The dashboard. |

## Invoices

| Method | Path | Does |
| --- | --- | --- |
| `POST` | `/invoices` | Creates and sends an invoice from the form. Fields: `client_name`, `client_email`, `description`, `amount`, `currency`, `terms_days`. Optional: `repeat` (`WEEK` or `MONTH`) with `repeat_cycles`, or `instalments` (2 to 6) with `instalment_interval`. Redirects to `/` on success. Returns the page with field errors and `422` on bad input, or `502` when PayPal fails. If PayPal created the invoice but the send failed, the invoice stays a `DRAFT` with its PayPal ID, and a ticked Repeat is kept on it until the invoice is sent. |
| `POST` | `/parse` | Field `description`, the free text. Returns `{"proposal": {...}, "missing": [...]}` with the six form fields. A field the model left out is an empty string and is named in `missing`. Creates nothing. `422` if the answer has an unknown key or a wrongly typed value, a filled field fails validation, or the live model reply cannot be read (not JSON, not an object, or no text content). The error message never repeats the reply. |
| `GET` | `/api/invoices` | The user's invoices as dashboard rows. |
| `POST` | `/api/invoices/{invoice_id}/refresh` | Reads the status from PayPal and stores it if it moves the invoice forward (see [statuses](data-model.md#invoice)). A sent, overdue or paid invoice starts any Repeat held on it and marks the scheduled invoice linked to it `SENT`. Returns `{"status": ...}` with the stored status. |
| `POST` | `/api/invoices/{invoice_id}/send` | Sends a `DRAFT` that PayPal already holds, after a failed send. It reuses the PayPal invoice and never creates another. If the form's Repeat was ticked, the first send that succeeds starts the schedule. If the draft is linked to a scheduled invoice, that item is marked `SENT` and its schedule finishes if nothing is left. Returns `status`, and a `warning` if PayPal did not confirm the send in time; the invoice then counts as sent. `409` unless the invoice is a `DRAFT` with a PayPal ID, or while its scheduled invoice is being approved or sent from another tab. While this send runs, an approval of that item gets `409`. `502` if PayPal fails; the invoice stays a draft and its scheduled invoice stays pending. |
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
| `POST` | `/api/reminders/{reminder_id}/approve` | Field `channel`: `paypal` (the default), `email` or `both`. Sends through each channel not yet used. Returns `status`, `sent_channels`, and a `warning` if PayPal did not confirm in time. `409` if the draft is not pending, another approval holds it, or the invoice has no PayPal ID. `409` too if the invoice is paid, cancelled or refunded; the draft is then marked `REJECTED`. `502` if a channel fails; the draft stays pending. |
| `POST` | `/api/reminders/{reminder_id}/reject` | Marks the draft `REJECTED`. A later scan may draft a new one. |

## Scheduled invoices

| Method | Path | Does |
| --- | --- | --- |
| `POST` | `/api/scheduled/{item_id}/approve` | Creates and sends a pending scheduled invoice. The item is linked to its invoice before PayPal is called, so a retry never creates a second one: if an earlier try saved the invoice but stopped before PayPal created it, creates that one in PayPal; if PayPal holds it unsent, sends it; if PayPal has since sent it, marks the item sent. If the linked invoice was cancelled or refunded before it was sent, the item is unlinked from it and a new invoice is created and sent. Returns `status`, `invoice_id`, and a `warning` if PayPal did not confirm the send in time. `409` if the item is not pending, another approval or a Send on its invoice row holds it, or its schedule is not active. `502` if PayPal fails; the item goes back to pending, unless its schedule was cancelled meanwhile, when it stays cancelled. |
| `POST` | `/api/scheduled/{item_id}/reject` | Skips this invoice. A repeat carries on. `409` if the item is not pending, including when an approval or a Send claims it while the skip runs; if PayPal holds its linked invoice as an unsent draft (send it from its invoice row, or cancel it in PayPal and refresh it, first), or if PayPal has already sent the linked invoice. A linked draft that PayPal never took is deleted. |
| `POST` | `/api/schedules/{schedule_id}/cancel` | Stops the schedule and cancels its upcoming and pending invoices, and any an approval is sending. A pending invoice's linked draft that PayPal never took is deleted. Sent invoices stay as they are, even one an approval marks sent while the cancel runs. |

## PayPal webhook

In `mock` mode the webhook returns `401` with `{"ok": false, "error": "webhooks are off in mock mode"}` and stores nothing, unless `MOCK_WEBHOOKS` is on ([configuration](configuration.md#modes)).

| Method | Path | Does |
| --- | --- | --- |
| `POST` | `/webhooks/paypal` | Public. Checks the signature through PayPal before anything else. Stores each event once by its PayPal event ID, so a replay changes nothing, even one that arrives while the first copy is still being handled. `400` for a bad signature, a body that is not UTF-8, invalid JSON or a missing event ID. `413` above 1 MB. |

The webhook maps these PayPal events to invoice statuses: `INVOICING.INVOICE.SENT`, `.PAID`, `.CANCELLED` and `.REFUNDED`. The event belongs to the user who owns the invoice. Duebell still stores an event that would move the invoice backward, such as `SENT` after `PAID`, but the event changes nothing and returns `"applied": false`. An event that moves a draft with a held Repeat to sent or paid starts the repeat's schedule; one that cancels or refunds it drops the repeat. An event that moves a draft linked to a scheduled invoice to sent or paid marks that item `SENT`.
