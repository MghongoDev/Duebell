# 0007 Scheduled invoices wait for approval

## Status

Accepted

## Date

2026-11-05

## Context

Recurring invoices and instalment plans (issue #25) create invoices on a timer. ADR 0003 settled that a reminder never leaves the app without a click, because the scheduler runs unattended and a wrong message costs a client relationship. A wrong invoice costs more: it asks for money. PayPal has its own recurring series, which sends each invoice by itself once activated.

## Decision

Keep the timer local. A `Schedule` holds the rule, and each future invoice is a `ScheduledInvoice`. The scan moves a due one from UPCOMING to PENDING. Only an approval creates it, through the same `create_invoice` path as the form, so it gets risk scoring, a PayPal ID, reminders and history like any other invoice. Rejecting one skips it, and a repeat carries on. Cancelling a schedule stops future invoices and leaves sent ones alone.

The form's first submission is itself the confirmation: a repeating invoice sends its first invoice at once. An instalment plan sends nothing until its first instalment is approved.

## Alternatives considered

- **PayPal recurring series.** Rejected because PayPal would send each invoice without a click, and the local history would only learn about it from webhooks.
- **Send scheduled invoices automatically and let the operator cancel.** Rejected for the reason ADR 0003 gives: silence is not approval.
- **PayPal partial payments on one invoice instead of instalments.** Rejected because the client would pick the amounts and dates, and Duebell could not remind on a missed instalment.

## Consequences

- Nothing on a schedule reaches PayPal without a click.
- An operator who stops checking the queue stops invoicing. The pending list makes that visible.
- Paid instalments feed the client's history and the risk refit like any invoice.

## Revision, 2026-11-05

A review found two gaps.

- The form offered instalments only for a High-risk invoice, but the server accepted a split at any risk. The server now scores the invoice first and refuses a split unless it is High. It checks before creating the client, so a refused split leaves nothing behind.
- A repeat with a fixed number of cycles went DONE as soon as it queued its last invoice, while that invoice still waited for approval. It then dropped off the dashboard and could not be cancelled. A schedule now goes DONE only when it can queue nothing more and none of its invoices is upcoming or pending.

A third fix follows from the approval rule. If PayPal created an invoice but sending it failed, approving again used to create a second invoice. The scheduled invoice now keeps a link to the one PayPal holds, and the retry sends that one.
