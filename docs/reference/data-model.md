# Data model

Duebell keeps everything in one SQLite database. The tables live in `app/models.py`. At start, `init_db` creates missing tables, then adds any columns and indexes a newer build declares, so a database from an older build keeps working. It never renames or drops anything. If existing rows break a new unique index, `init_db` leaves the rows alone, logs a warning that names the index, and tries again at the next start. The app still starts.

Every table that holds a user's data has a `user_id` ([ADR 0008](../adr/0008-sign-in-and-ownership.md)). Terms in **bold** are defined in the [glossary](../../GLOSSARY.md).

## Tables

| Table | One row is | Key fields |
| --- | --- | --- |
| `user` | A person who signs in. | `email` (unique), `password_hash` (scrypt, with its cost). |
| `client` | Someone a user bills. | `name`, `email`. Email is unique per user, so two users may bill the same address. |
| `invoice` | A request for payment. | `invoice_number`, `paypal_invoice_id`, `amount_cents`, `currency`, `terms_days`, `issued_at`, `due_at`, `paid_at`, `status`, `risk_score`, `risk_level`, `risk_reasons`, `limited_data`, `limited_data_reason`, `repeat_unit`, `repeat_cycles`. The last two hold a Repeat ticked on the form while the invoice waits to be sent, and are cleared when the invoice is sent and the schedule starts, whether **Send**, a webhook or **Refresh** moved it, or when it is cancelled or refunded first. |
| `reminder` | A drafted or sent **reminder**. | `invoice_id`, `draft_text`, `tone`, `stage`, `status`, `sent_channels`, `sent_at`. At most one `PENDING` reminder per invoice. |
| `event` | One PayPal webhook delivery. | `paypal_event_id` (primary key), `event_type`, `payload`. |
| `schedule` | A **schedule**: a repeat or an **instalment plan**. | `kind`, `client_id`, `amount_cents`, `currency`, `terms_days`, `interval_unit`, `interval_count`, `cycles_left`, `starts_on`, `status`. |
| `scheduledinvoice` | A **scheduled invoice**. | `schedule_id`, `sequence`, `amount_cents`, `issue_on`, `status`, `invoice_id`. `schedule_id` and `sequence` together are unique. |
| `riskcomparison` | One refit of the **risk comparison**. | `status`, `rows_used`, `late_count`, `test_rows`, `baseline_accuracy`, `baseline_auc`, `logistic_accuracy`, `logistic_auc`, `message`. |

Amounts are whole minor units of their currency, despite the `_cents` name: cents for USD, yen for JPY. So 12.50 USD is stored as `1250` and 1000 JPY as `1000`. Times are stored in UTC, and every time column reads back as an aware UTC datetime, whichever sqlmodel version is installed.

An invoice's `due_at` is its `issued_at` plus `terms_days`. Duebell sends PayPal that date as the invoice's `due_date`, so the client sees the same **due date** that reminders and the overdue flag use. Terms of 0 days are sent as due on receipt.

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
| `PAID`, `MARKED_AS_PAID` | Paid. Both count toward history, collected totals and the risk comparison. |
| `CANCELLED`, `REFUNDED` | Closed without payment, or refunded. |
| `OVERDUE` | Duebell never sets it, but a refresh stores it if PayPal reports it. The scan and the outstanding total treat it like `SENT`. |

A status only moves forward. A webhook or refresh never moves an invoice out of `PAID`, `MARKED_AS_PAID`, `CANCELLED` or `REFUNDED`, except from paid to `REFUNDED`, and never moves it back to `DRAFT`. A refresh ignores any status not in this table, such as `UNKNOWN`. The dashboard's overdue flag is computed: an invoice is overdue when it is past its due date and not paid, cancelled or refunded.

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

`stage` is the **stage** the invoice was in when the scan drafted the reminder: `BEFORE_DUE`, or `OVERDUE` once the invoice is a full day past its `due_at`. An invoice gets at most one `SENT` reminder per stage. A `PENDING` reminder blocks any new draft for its invoice, and a `REJECTED` one blocks nothing. A row from a build before stages has an empty `stage`. The scan counts it as `OVERDUE` if its `created_at` is a full day past the invoice's `due_at`, and as `BEFORE_DUE` otherwise. Startup adds the column to an older database and leaves it empty on old rows.

The unique index `ux_reminder_pending_invoice` allows one `PENDING` reminder per invoice. If two scans draft for the same invoice at once, the database refuses the second draft and that scan skips the invoice.

### Schedule

| Status | Meaning |
| --- | --- |
| `ACTIVE` | May still produce invoices, or has one waiting for approval. |
| `DONE` | Has nothing left to produce and nothing open. |
| `CANCELLED` | Stopped by the user. |

`kind` is `RECURRING` or `INSTALMENT`. For a repeat, `cycles_left` counts the invoices still to queue, or is empty for no end. `starts_on` is the date of the invoice that started a repeat. Occurrence n falls n intervals after it, clamped to the length of the month. A repeat from an older build has no `starts_on`, so it counts from its earliest scheduled invoice.

### Scheduled invoice

`UPCOMING`, then `PENDING` when the scan finds it due. The scan claims the item by moving it from `UPCOMING` to `PENDING` in one conditional update, and only the scan that wins queues the next occurrence. The unique index `ux_scheduledinvoice_schedule_sequence` refuses a second item with the same `schedule_id` and `sequence`. If the next occurrence already exists, the item still moves to `PENDING` and nothing new is queued. After `PENDING` comes `SENDING` while one approval talks to PayPal, then `SENT`. Approval claims the item by moving it from `PENDING` to `SENDING` in one conditional update, so a second approval at the same moment gets a 409. A failed send puts it back to `PENDING`, and so does a restart that finds it `SENDING`. It can also end `REJECTED` (skipped) or `CANCELLED` (its schedule was cancelled). `invoice_id` links the invoice it became. It is set before `SENT` if PayPal created the draft but the send failed.

### Risk comparison

`OK` with the four scores, or `NOT_ENOUGH_HISTORY` with a message saying how many invoices it found.
