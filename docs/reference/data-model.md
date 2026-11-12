# Data model

Duebell keeps everything in one SQLite database. The tables live in `app/models.py`. At start, `init_db` creates missing tables, then adds any columns and indexes a newer build declares, so a database from an older build keeps working. It never renames or drops anything. If existing rows break a new unique index, `init_db` leaves the rows alone, logs a warning that names the index, and tries again at the next start. The app still starts.

Every table that holds a user's data has a `user_id` ([ADR 0008](../adr/0008-sign-in-and-ownership.md)). Terms in **bold** are defined in the [glossary](../../GLOSSARY.md).

## Tables

| Table | One row is | Key fields |
| --- | --- | --- |
| `user` | A person who signs in. | `email` (unique), `password_hash` (scrypt, with its cost), `session_key` (random; every session cookie carries it, and a cookie whose key no longer matches is refused. Startup gives a key to any user without one). |
| `client` | Someone a user bills. | `name`, `email`. Email is unique per user, so two users may bill the same address. |
| `invoice` | A request for payment. | `invoice_number`, `paypal_invoice_id`, `amount_cents`, `currency`, `terms_days`, `issued_at`, `due_at`, `paid_at`, `status`, `risk_score`, `risk_level`, `risk_reasons`, `limited_data`, `limited_data_reason`, `repeat_unit`, `repeat_cycles`. The last two hold a Repeat ticked on the form while the invoice waits to be sent, and are cleared when the invoice is sent and the schedule starts, whether **Send**, a webhook or **Refresh** moved it, or when it is cancelled or refunded first. |
| `invoicenumbercounter` | The counter behind invoice numbers. One row. | `last`, the last number handed out. |
| `reminder` | A drafted or sent **reminder**. | `invoice_id`, `draft_text`, `tone`, `chosen_tone`, `stage`, `status`, `sent_channels`, `paypal_attempted_at`, `email_attempted_at`, `sent_at`. At most one `PENDING` reminder per invoice. |
| `event` | One PayPal webhook delivery, or one simulated payment. | `paypal_event_id` (primary key), `user_id`, `event_type`, `payload`, `received_at`. A simulated payment stores a row with `event_type` `Simulated payment`, an ID that starts with `SIMULATED-` and names the invoice, and no payload. It is the owner's activity feed line, not a PayPal event. |
| `schedule` | A **schedule**: a repeat or an **instalment plan**. | `kind`, `client_id`, `amount_cents`, `currency`, `terms_days`, `interval_unit`, `interval_count`, `cycles_left`, `starts_on`, `status`. |
| `scheduledinvoice` | A **scheduled invoice**. | `schedule_id`, `sequence`, `amount_cents`, `issue_on`, `status`, `invoice_id`. `schedule_id` and `sequence` together are unique. |
| `riskcomparison` | One refit of the **risk comparison**. | `status`, `rows_used`, `late_count`, `test_rows`, `baseline_accuracy`, `baseline_auc`, `logistic_accuracy`, `logistic_auc`, `message`. |

Amounts are whole minor units of their currency, despite the `_cents` name: cents for USD, yen for JPY. So 12.50 USD is stored as `1250` and 1000 JPY as `1000`. Times are stored in UTC, and every time column reads back as an aware UTC datetime, whichever sqlmodel version is installed.

An invoice's `due_at` is its `issued_at` plus `terms_days`. Duebell sends PayPal that date as the invoice's `due_date`, so the client sees the same **due date** that reminders and the overdue flag use. Terms of 0 days are sent as due on receipt.

## Invoice numbers

An invoice number looks like `DUE-20261111-0042`: the date it was saved, then a counter. Numbers are shared by all users. The counter in `invoicenumbercounter` only goes up and stays above every invoice id, so Duebell never hands out a number twice, even after it deletes a draft that PayPal refused. That matters because PayPal may have created the invoice and lost its answer: it then holds the number and refuses any later invoice that reuses it. Duebell takes the number in the same transaction that saves the invoice, so two requests at once get different numbers. On a database from an older build, startup adds the counter above the highest invoice id and the highest number at the end of any invoice number. Existing invoice numbers do not change.

## Statuses

Each set of values below has its own type in the code. The database stores the plain strings, so a database from an older build reads the same.

| Values | Type |
| --- | --- |
| Invoice status | `InvoiceStatus` |
| Reminder status | `ReminderStatus` |
| Reminder stage | `ReminderStage` |
| Schedule status, schedule kind | `ScheduleStatus`, `ScheduleKind` |
| Schedule interval | `IntervalUnit` |
| Scheduled invoice status | `ScheduledInvoiceStatus` |
| Risk comparison status | `ComparisonStatus` |
| Risk level | `RiskLevel` |
| Reminder channel | `Channel` |

### Invoice

| Status | Meaning |
| --- | --- |
| `DRAFT` | Saved locally. If it has a `paypal_invoice_id`, PayPal holds a draft that has not been sent yet. |
| `SENT` | PayPal sent it to the client. |
| `PAID`, `MARKED_AS_PAID`, `PAID_EXTERNAL` | Paid. `PAID_EXTERNAL` means the user recorded a payment made outside PayPal. All three set `paid_at`, close the invoice, and count toward history, collected totals and the risk comparison. |
| `CANCELLED`, `AUTO_CANCELLED` | Closed without payment. PayPal sets `AUTO_CANCELLED` when no payment arrives in time. Neither counts as history. |
| `REFUNDED`, `MARKED_AS_REFUNDED`, `REFUNDED_EXTERNAL` | Refunded, through PayPal, marked by the user, or outside PayPal. Closed. |
| `OVERDUE` | Duebell never sets it, but a refresh stores it if PayPal reports it. The scan and the outstanding total treat it like `SENT`. |
| `PAYMENT_PENDING` | The client paid, but PayPal holds the payment for review. Open. |
| `PARTIALLY_PAID` | The client paid part of the amount. Open. |
| `PARTIALLY_REFUNDED` | The user refunded part of a payment. Open. |

The three partial statuses are open: the invoice still counts toward the outstanding total, the scan can draft reminders for it, and a webhook or refresh can still move it on. Duebell does not store how much was paid or refunded, so the outstanding total counts the full amount, and the paid total leaves the invoice out. The scan skips an invoice that has `paid_at` set, so a paid invoice that is then partly refunded gets no reminder. None of the three sets `paid_at`, so the risk score counts a partly paid invoice as unpaid.

A webhook takes the status from the invoice the event carries. PayPal sends `INVOICING.INVOICE.PAID` for a full payment, a part payment or a pending one, and `INVOICING.INVOICE.REFUNDED` for a full or part refund. Only when the invoice carries no status does Duebell use the event type: `SENT`, `PAID`, `CANCELLED` or `REFUNDED`.

Duebell stores each status as PayPal names it, and treats `PAID_EXTERNAL` as paid, `MARKED_AS_REFUNDED` and `REFUNDED_EXTERNAL` as refunded, and `AUTO_CANCELLED` as cancelled. Any of them closes the invoice and rejects its pending reminder drafts.

A status only moves forward. A webhook, a send, a refresh or a simulated payment applies its status only if it may replace the status stored at that moment, so a status a webhook saves during a PayPal call stays. A webhook or refresh never moves an invoice out of a paid, refunded or cancelled status, except from paid to `PARTIALLY_REFUNDED` or to a refunded status, and never moves it back to `DRAFT`. Money in runs `SENT` to `PARTIALLY_PAID` to `PAID`: a partly paid invoice never goes back to `SENT` or `OVERDUE`. `PAYMENT_PENDING` may come before or after a part payment, and may go back to `SENT` if the payment fails. Money out runs paid to `PARTIALLY_REFUNDED` to `REFUNDED`: a partly refunded invoice can only become a refunded status. A webhook or refresh ignores any status not in this table, such as `UNKNOWN`. It also ignores three PayPal statuses: `UNPAID`, `SCHEDULED` and `SHARED`. None of them closes the invoice or changes what the client owes. The dashboard's overdue flag is computed: an invoice is overdue once the UTC day of its `due_at` has ended and it is not a draft, paid, cancelled or refunded, so a partial status can be overdue. That day is the `due_date` PayPal shows the client, so an invoice is not overdue on its due date itself. A draft was never sent, so the client owes nothing yet. The status filter's Overdue option shows rows with this flag and rows in PayPal's `OVERDUE` status. An invoice row offers **Send**, **Refresh** and **Simulate payment** only when the invoice has a `paypal_invoice_id`: **Send** on a `DRAFT`, **Refresh** always, and **Simulate payment** until the invoice is paid, cancelled or refunded.

`risk_reasons` holds one reason per line. `limited_data_reason` is the reason that says why a score has limited data, set when the invoice is scored. It is empty when the score has enough data, and the dashboard shows it under the "Limited data" label. Startup adds the column to an older database and leaves it empty on old rows, so they show "Limited data" alone.

### Reminder

| Status | Meaning |
| --- | --- |
| `PENDING` | A draft waiting for approval. `sent_channels` may already list a channel if a later one failed. |
| `SENDING` | One approval has claimed it and is sending. A second approval gets a `409`. |
| `SENT` | Every chosen channel delivered it. |
| `REJECTED` | Dropped without sending. A later scan may draft a new one. |

Approval claims the draft by moving it from `PENDING` to `SENDING` in one conditional update. A failed channel puts it back to `PENDING`, and so does a restart that finds it `SENDING`. A scan drafts nothing for an invoice whose reminder is `SENDING`.

`sent_channels` lists the channels that delivered the reminder, for example `paypal,email`. A retry skips them. `sent_at` is when a channel last delivered.

`paypal_attempted_at` is when Duebell last asked PayPal to send the reminder. It is saved before the call. If it is set but `sent_channels` lacks PayPal, PayPal may have sent the reminder, so a retry skips PayPal and warns the user to check PayPal. If PayPal refuses the call, Duebell clears it and a retry sends through PayPal. It never counts a reminder as delivered: the activity feed, the dashboard, and the scan read only `status` and `sent_channels`.

`email_attempted_at` is when Duebell last handed the reminder to the mail server. It is saved before the send. If it is set but `sent_channels` lacks email, the server may have taken the email, so a retry skips email and warns the user to check their sent mail. If the server refused the email before it took the message (no connection, failed login, refused sender or recipient, or a rejected message), Duebell clears it and a retry sends the email. A lost answer after the message went out counts the email as sent, with a warning. Like `paypal_attempted_at`, it never counts a reminder as delivered.

`stage` is the **stage** the invoice was in when the scan drafted the reminder: `BEFORE_DUE`, or `OVERDUE` once the scan runs after the UTC day of the invoice's `due_at` has ended. An invoice gets at most one sent reminder per stage. A reminder counts as sent when its status is `SENT` or its `sent_channels` is not empty, so a `REJECTED` row that PayPal or email delivered still counts. A `PENDING` reminder blocks any new draft for its invoice, and a `REJECTED` one with empty `sent_channels` blocks nothing. A row from a build before stages has an empty `stage`. The scan counts it as `OVERDUE` if its `created_at` falls after the UTC day of the invoice's `due_at`, and as `BEFORE_DUE` otherwise. Startup adds the column to an older database and leaves it empty on old rows.

`tone` is the tone of the current text: `friendly` or `firm`, or `custom` once the user edits it by hand. The scan sets it to `firm` for a High-risk invoice or one more than 14 days overdue, and `friendly` otherwise. `chosen_tone` is the tone the user picked with the tone switch, `friendly` or `firm`. It is empty if the user never picked one, so an empty `chosen_tone` means the scan chose the tone. A later hand edit sets `tone` to `custom` and leaves `chosen_tone` as it was. Startup adds the column to an older database and leaves it empty on old rows.

The unique index `ux_reminder_pending_invoice` allows one `PENDING` reminder per invoice. If two scans draft for the same invoice at once, the database refuses the second draft and that scan skips the invoice.

### Schedule

| Status | Meaning |
| --- | --- |
| `ACTIVE` | May still produce invoices, or has one waiting for approval. |
| `DONE` | Has nothing left to produce and nothing open. |
| `CANCELLED` | Stopped by the user. |

`kind` is `RECURRING` or `INSTALMENT`. For a repeat, `cycles_left` counts the invoices still to queue, or is empty for no end. `starts_on` is the date of the invoice that started a repeat. Occurrence n falls n intervals after it, clamped to the length of the month. A repeat from an older build has no `starts_on`, so it counts from its earliest scheduled invoice.

### Scheduled invoice

`UPCOMING`, then `PENDING` when the scan finds it due. The scan claims the item by moving it from `UPCOMING` to `PENDING` in one conditional update, and only the scan that wins queues the next occurrence. The unique index `ux_scheduledinvoice_schedule_sequence` refuses a second item with the same `schedule_id` and `sequence`. If the next occurrence already exists, the item still moves to `PENDING` and nothing new is queued. After `PENDING` comes `SENDING` while one approval talks to PayPal, then `SENT`. Approval claims the item by moving it from `PENDING` to `SENDING` in one conditional update, so a second approval at the same moment gets a 409. A failed send puts it back to `PENDING` with one conditional update, only if it is still `SENDING`, and so does a restart that finds it `SENDING`. Cancelling the schedule moves a `SENDING` item to `CANCELLED`, and a failed send then leaves it there. It can also end `REJECTED` (skipped) or `CANCELLED` (its schedule was cancelled). `invoice_id` links the invoice it became. Approval saves the invoice as a `DRAFT` and commits this link before it calls PayPal, so a retry after a failed commit or a restart finishes the same invoice. If PayPal refuses to create the invoice, the draft is deleted and the link cleared, even if the schedule was cancelled meanwhile. If PayPal created the invoice but did not send it, the draft and the link stay; on a cancelled schedule the user can send it from its row or leave it. When the linked invoice reaches `SENT`, `OVERDUE`, a partial status or a paid status by any path (**Send**, a webhook or **Refresh**), one conditional update moves a `PENDING`, `REJECTED` or `CANCELLED` item to `SENT`; a `SENDING` item is left to its approval. If the linked invoice is `CANCELLED` or `REFUNDED` before it was sent, approval points `invoice_id` at a new invoice and leaves the old one as it is. Skipping is refused while the linked invoice is a `DRAFT` with a PayPal ID, or once it is sent.

### Risk comparison

`OK` with the four scores, or `NOT_ENOUGH_HISTORY` with a message saying how many invoices it found. The scores come from the newest quarter of invoices by issue date, and `test_rows` counts them. An `OK` message names the issue dates the comparison held out. A refit is also `NOT_ENOUGH_HISTORY` when the held-out invoices, or the older ones it trains on, are all late or all on time.
