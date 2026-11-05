# Approvals and sending

Nothing Duebell sends to a client leaves without a person's click. That holds for reminders ([ADR 0003](../adr/0003-human-approval.md)) and for invoices a schedule produces ([ADR 0007](../adr/0007-scheduled-invoices-need-approval.md)). The scheduler drafts; a person sends.

The reason is cost. A wrong reminder can damage a client relationship, and a wrong invoice asks for money. Risk predicts lateness, not whether a message suits today. Silence is not approval, so nothing sends after a delay either.

## What needs a click

| Action | Drafted by | Sent by |
| --- | --- | --- |
| The first invoice | The user, on the form | Submitting the form |
| A reminder | The scan | `Approve and send` |
| A repeat's next invoice | The scan, when its date comes | `Approve and send` |
| An instalment | The plan, when its date comes | `Approve and send` |

The scan runs every 30 minutes and on `Scan now`. It drafts a reminder for a sent invoice that is overdue, or due within the reminder window and scored Medium or High. It skips invoices with no PayPal ID, because a draft for them could never be sent. It also skips invoices that already have a pending or sent reminder.

## Channels

When approving a reminder, the user picks a [channel](../../GLOSSARY.md): PayPal, email, or both. PayPal goes first.

Duebell records each channel the moment it succeeds. If email then fails, the reminder stays pending with an error, and approving it again sends only the email. PayPal never sends the same reminder twice. The activity feed lists a reminder once any channel has delivered it, even if the rest of the draft is later rejected.

## Never sending twice

Three failures could each lead to a duplicate. Duebell handles each one differently.

| Failure | What could go wrong | What Duebell does |
| --- | --- | --- |
| PayPal creates an invoice, then the send fails | A retry creates a second invoice. | The invoice stays a draft with its PayPal ID. Approving again sends that draft. |
| Email fails after PayPal sent the reminder | A retry reminds through PayPal again. | `sent_channels` records PayPal, and the retry skips it. |
| A toolkit call times out | The call keeps running and may still send. A retry sends again. | It counts the PayPal reminder as sent and warns the user to check PayPal. |

The third case trades one possible missed reminder for no duplicate. A duplicate reaches the client. A missed one shows up when the user checks PayPal, and they can remind from there. Duebell will not draft another reminder for that invoice, because it counts this one as sent.

One gap remains. The sandbox gateway retries timeouts and server errors up to three times, including the call that sends a reminder. If PayPal acted on a request whose answer was lost, the retry repeats it. Many PayPal APIs accept a `PayPal-Request-Id` header that makes a retry safe. Duebell does not send one, and nobody has tested whether the Invoicing endpoints honour it.

## Schedules

A repeat sends its first invoice when the user submits the form. A schedule then queues the next one. When its date comes, the scan moves it to pending. Approving it creates the invoice through the same path as the form, so it gets a risk score, reminders and history like any other.

Skipping one leaves the repeat running. Cancelling the schedule cancels everything upcoming or pending and leaves sent invoices alone. A schedule stays active, and can be cancelled, until its last invoice is approved or skipped.

The form offers an instalment plan only when the invoice scores High. The server checks the same rule. A plan sends nothing until the user approves its first instalment.
