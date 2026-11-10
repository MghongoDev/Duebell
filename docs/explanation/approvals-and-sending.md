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

The scan runs every 30 minutes and on `Scan now`. It drafts a reminder for a sent invoice that is overdue, or due within the reminder window and scored Medium or High. It skips invoices with no PayPal ID, because a draft for them could never be sent. An invoice can get two reminders: one before it is due and one once it is overdue. Each reminder records its [stage](../../GLOSSARY.md) when the scan drafts it: overdue once the invoice is a full day past its due date, before due until then. The scan skips an invoice that has a pending draft, whatever its stage, and an invoice that already has a sent reminder in the stage it would draft now. A rejected draft blocks nothing. So a Medium-risk invoice that got its friendly reminder still gets an overdue draft once it is late, and never a second reminder in either stage.

A reminder from a build that did not record stages counts as overdue if it was drafted a full day past the due date, and as before due otherwise.

The scheduler and `Scan now` can run at the same moment. Each still acts once. A scan claims a due scheduled invoice with one conditional update before it queues the next one, so a repeat never gains an extra invoice. The database allows one pending reminder per invoice, so a second scan's draft is refused and skipped.

Once an invoice is paid, cancelled or refunded, its pending drafts are rejected: by the webhook or refresh that closes it, by a simulated payment, and by the next scan. Approving a draft for a closed invoice is refused and rejects the draft, so a client never gets a reminder for an invoice they have paid.

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

The third case trades one possible missed reminder for no duplicate. A duplicate reaches the client. A missed one shows up when the user checks PayPal, and they can remind from there. Duebell will not draft another reminder for that invoice in the same stage, because it counts this one as sent.

### Retries inside one call

The sandbox gateway tries a call up to three times when it times out or PayPal returns a server error. If PayPal acted on a request whose answer was lost, a plain retry would repeat it. So every POST and PUT carries a `PayPal-Request-Id` header, chosen once per call and kept for all its retries. Where PayPal honours the header, it treats a repeated ID as the same request. The webhook signature check is a POST too, so it carries one, though repeating it does no harm.

How far that protects depends on the endpoint:

| Call | Is the header documented? |
| --- | --- |
| Create an invoice | Yes. PayPal's troubleshooting guide says a repeated ID returns the original result instead of a new invoice. |
| Send an invoice, send a reminder, record a payment | No. Duebell sends the header anyway, as PayPal's general guidance advises for every POST. Whether these endpoints honour it is unchecked. |

Toolkit calls carry no request ID, because the toolkit builds its own requests. They do not retry either, so the timeout rule above covers them. A live sandbox check (#32) will confirm which endpoints honour the header.

## Schedules

A repeat sends its first invoice when the user submits the form. A schedule then queues the next one. Each date counts from the first invoice, not from the date before it, so a short month does not pull later months back. When its date comes, the scan moves it to pending. Approving it creates the invoice through the same path as the form, so it gets a risk score, reminders and history like any other.

An approval claims the item before it calls PayPal, so a double click or a second tab cannot bill the client twice. If PayPal does not confirm a send in time, Duebell counts the invoice as sent and warns you to check PayPal, the same rule it applies to reminders. Sending again could bill the client twice; counting it as sent risks only an invoice you have to send by hand.

Skipping one leaves the repeat running. Cancelling the schedule cancels everything upcoming or pending and leaves sent invoices alone. A schedule stays active, and can be cancelled, until its last invoice is approved or skipped.

The form offers an instalment plan only when the invoice scores High. The server checks the same rule. A plan sends nothing until the user approves its first instalment.
