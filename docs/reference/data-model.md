# Data model

Duebell keeps everything in one SQLite database. The tables live in `app/models.py`. At start, `init_db` creates missing tables, then adds any columns and indexes a newer build declares, so a database from an older build keeps working. It never renames or drops anything.

Every table that holds a user's data has a `user_id` ([ADR 0008](../adr/0008-sign-in-and-ownership.md)). Terms in **bold** are defined in the [glossary](../../GLOSSARY.md).

## Tables

| Table | One row is | Key fields |
| --- | --- | --- |
| `user` | A person who signs in. | `email` (unique), `password_hash` (scrypt, with its cost). |
| `client` | Someone a user bills. | `name`, `email`. Email is unique per user, so two users may bill the same address. |
| `invoice` | A request for payment. | `invoice_number`, `paypal_invoice_id`, `amount_cents`, `currency`, `terms_days`, `issued_at`, `due_at`, `paid_at`, `status`, `risk_score`, `risk_level`, `risk_reasons`, `limited_data`. |
| `reminder` | A drafted or sent **reminder**. | `invoice_id`, `draft_text`, `tone`, `status`, `sent_channels`, `sent_at`. |
| `event` | One PayPal webhook delivery. | `paypal_event_id` (primary key), `event_type`, `payload`. |
| `schedule` | A **schedule**: a repeat or an **instalment plan**. | `kind`, `client_id`, `amount_cents`, `currency`, `terms_days`, `interval_unit`, `interval_count`, `cycles_left`, `status`. |
| `scheduledinvoice` | A **scheduled invoice**. | `schedule_id`, `sequence`, `amount_cents`, `issue_on`, `status`, `invoice_id`. |
| `riskcomparison` | One refit of the **risk comparison**. | `status`, `rows_used`, `late_count`, `test_rows`, `baseline_accuracy`, `baseline_auc`, `logistic_accuracy`, `logistic_auc`, `message`. |

Amounts are whole cents. Times are stored in UTC.

## Statuses

Each set of values below has its own type in the code. The database stores the plain strings, so a database from an older build reads the same.

| Values | Type |
| --- | --- |
| Invoice status | `InvoiceStatus` |
| Reminder status | `ReminderStatus` |
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

A refresh stores whatever status PayPal reports. The dashboard's overdue flag is computed: an invoice is overdue when it is past its due date and not paid, cancelled or refunded.

### Reminder

| Status | Meaning |
| --- | --- |
| `PENDING` | A draft waiting for approval. `sent_channels` may already list a channel if a later one failed. |
| `SENT` | Every chosen channel delivered it. |
| `REJECTED` | Dropped without sending. A later scan may draft a new one. |

`sent_channels` lists the channels that delivered the reminder, for example `paypal,email`. A retry skips them. `sent_at` is when a channel last delivered.

### Schedule

| Status | Meaning |
| --- | --- |
| `ACTIVE` | May still produce invoices, or has one waiting for approval. |
| `DONE` | Has nothing left to produce and nothing open. |
| `CANCELLED` | Stopped by the user. |

`kind` is `RECURRING` or `INSTALMENT`. For a repeat, `cycles_left` counts the invoices still to queue, or is empty for no end.

### Scheduled invoice

`UPCOMING`, then `PENDING` when the scan finds it due, then `SENT` once approved. It can also end `REJECTED` (skipped) or `CANCELLED` (its schedule was cancelled). `invoice_id` links the invoice it became. It is set before `SENT` if PayPal created the draft but the send failed.

### Risk comparison

`OK` with the four scores, or `NOT_ENOUGH_HISTORY` with a message saying how many invoices it found.
