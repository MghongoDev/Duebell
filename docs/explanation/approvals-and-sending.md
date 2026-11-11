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

The scan runs every 30 minutes and on `Scan now`. It drafts a reminder for a sent invoice that is overdue, or due within the reminder window and scored Medium or High. It skips invoices with no PayPal ID, because a draft for them could never be sent. An invoice can get two reminders: one before it is due and one once it is overdue. An invoice is overdue the moment its due time passes, the same test the dashboard's overdue flag uses. The scan, the stage and the wording all use it. Each reminder records its [stage](../../GLOSSARY.md) when the scan drafts it: overdue once the invoice is past its due time, before due until then. A before-due reminder says the invoice "is due on" its due date; an overdue one says it "was due on" that date. So an invoice two hours past due gets an overdue reminder, and a Low-risk invoice, which waits until it is overdue, gets just one reminder. The scan skips an invoice that has a pending draft or one being sent, whatever its stage, and an invoice that already has a sent reminder in the stage it would draft now. A reminder counts as sent once any channel has delivered it, whatever its status. So if PayPal sends a reminder, email fails, and the user rejects the rest, the scan drafts nothing more in that stage. A rejected draft that no channel delivered blocks nothing. So a Medium-risk invoice that got its friendly reminder still gets an overdue draft once it is late, and never a second reminder in either stage.

A reminder from a build that did not record stages counts as overdue if it was drafted after the due time, and as before due otherwise.

The scheduler and `Scan now` can run at the same moment. Each still acts once. A scan claims a due scheduled invoice with one conditional update before it queues the next one, so a repeat never gains an extra invoice. The database allows one pending reminder per invoice, so a second scan's draft is refused and skipped.

Once an invoice is paid, cancelled or refunded, its pending drafts are rejected: by the webhook or refresh that closes it, by a simulated payment, and by the next scan. Approving a draft for a closed invoice is refused and rejects the draft, so a client never gets a reminder for an invoice they have paid.

Approving a reminder claims the draft before anything is sent, the same way a scheduled invoice is claimed. A double click or a second tab gets a `409`, so the client gets one reminder. If a channel fails, the claim is released and the draft is pending again.

Two copies of one PayPal webhook can arrive at the same moment. The webhook stores the event before it reads the invoice. The second copy waits for the first, finds the event ID taken, and counts as a duplicate. An event for the same invoice also waits, so the forward-only rule always compares against the latest status.

## Channels

When approving a reminder, the user picks a [channel](../../GLOSSARY.md): PayPal, email, or both. PayPal goes first.

Duebell records each channel the moment it succeeds. If email then fails, the reminder stays pending with an error, and approving it again sends only the email. PayPal never sends the same reminder twice. The activity feed lists a reminder once any channel has delivered it, even if the rest of the draft is later rejected. Such a rejected reminder's row names the channels that sent it, and it still counts as sent in its stage.

## Never sending twice

Five failures could each lead to a duplicate. Duebell handles each one differently.

| Failure | What could go wrong | What Duebell does |
| --- | --- | --- |
| PayPal creates an invoice, then the send fails | A retry creates a second invoice. | The invoice stays a draft with its PayPal ID. Approving again, or clicking **Send** on the draft's row, sends that draft. |
| Email fails after PayPal sent the reminder | A retry reminds through PayPal again. | `sent_channels` records PayPal, and the retry skips it. |
| A PayPal call times out | PayPal may have acted, or may still act. A retry sends again. | It counts the PayPal reminder or invoice as sent and warns the user to check PayPal. |
| PayPal sends the reminder, then Duebell fails to save that | A retry reminds through PayPal again. | Duebell saves `paypal_attempted_at` before it calls PayPal. The retry finds it, counts the PayPal reminder as sent, and warns the user to check PayPal. If PayPal refused the call, Duebell clears it, so the retry sends through PayPal. |
| The mail server takes the email, but its answer is lost | A retry emails the client again. | Duebell saves `email_attempted_at` before it hands the email over. If the connection fails, the login fails, the server refuses the sender or recipient, or it rejects the message, nothing went out: Duebell clears the attempt and a retry sends the email. Once the message itself has gone out, any other failure, such as a timeout while waiting for the server's answer, counts the email as sent and warns the user to check their sent mail. A retry that finds the attempt does the same. |

The last three cases trade one possible missed reminder for no duplicate. A duplicate reaches the client. A missed one shows up when the user checks PayPal or their sent mail, and they can remind from there. Duebell will not draft another reminder for that invoice in the same stage, because it counts this one as sent.

### Retries inside one call

The sandbox gateway tries a call up to three times when it times out or PayPal returns a server error. If PayPal acted on a request whose answer was lost, a plain retry would repeat it. So every POST and PUT carries a `PayPal-Request-Id` header, chosen once per call and kept for all its retries. Where PayPal honours the header, it treats a repeated ID as the same request. The webhook signature check is a POST too, so it carries one, though repeating it does no harm.

If a POST or PUT still fails and any attempt lost its answer after the request went out (a read timeout or a dropped connection), the gateway reports a timeout, as the toolkit does, and the rule above counts the call as sent. A refusal from PayPal after such an attempt counts the same way, since PayPal may be saying it already acted. A call that never reached PayPal, because the connection did not open, stays a plain error, so a retry sends it. Reads stay plain errors too.

How far that protects depends on the endpoint:

| Call | Is the header documented? |
| --- | --- |
| Create an invoice | Yes. PayPal's troubleshooting guide says a repeated ID returns the original result instead of a new invoice. |
| Send an invoice, send a reminder, record a payment | No. Duebell sends the header anyway, as PayPal's general guidance advises for every POST. Whether these endpoints honour it is unchecked. |

Toolkit calls carry no request ID, because the toolkit builds its own requests. They do not retry either. A toolkit call that runs past `PAYPAL_TIMEOUT_SECONDS` keeps running, so the timeout rule above covers it. A live sandbox check (#32) will confirm which endpoints honour the header.

## Schedules

A repeat sends its first invoice when the user submits the form. A schedule then queues the next one. If that send fails, the repeat waits on the draft and starts when the draft leaves `DRAFT` for a sent status, whether **Send**, a webhook or **Refresh** moved it. Each of them claims the waiting repeat with one conditional update, so a race starts one schedule. A draft that ends cancelled drops its repeat. Each date counts from the first invoice, not from the date before it, so a short month does not pull later months back. When its date comes, the scan moves it to pending. Approving it creates the invoice through the same path as the form, so it gets a risk score, reminders and history like any other. Approval saves the invoice and links it to the scheduled one before it calls PayPal. If a commit fails or the app restarts partway, the next approval finishes that invoice, so PayPal bills the client once.

A scheduled invoice and the invoice it is linked to never disagree. Approval counts the linked invoice as sent only if it is sent, overdue or paid. If the linked draft was cancelled or refunded before PayPal sent it, the client was never billed, so approval unlinks it, leaves it as it is, and bills the instalment with a new invoice. That is what approving means: send this one. A person who no longer wants it skips it instead. When a linked draft reaches a sent status any other way, through **Send** on its invoice row, a webhook or **Refresh**, the scheduled invoice is marked sent, and the schedule finishes if that was its last. This holds even if the item was skipped or its schedule cancelled meanwhile, since the client has the invoice. An item an approval holds is left to that approval.

An approval claims the item before it calls PayPal, so a double click or a second tab cannot bill the client twice. If PayPal does not confirm a send in time, Duebell counts the invoice as sent and warns you to check PayPal, the same rule it applies to reminders. Sending again could bill the client twice; counting it as sent risks only an invoice you have to send by hand.

Skipping one leaves the repeat running. Skipping is refused while PayPal holds the linked draft unsent, since **Send** on its invoice row could still bill the client: send it there, or cancel it in PayPal and refresh it, then skip. Duebell does not cancel the draft for you, because it never cancels anything in PayPal. Skipping is refused too once PayPal has sent the linked invoice; approve it to record it. A linked draft PayPal never took, left by a restart before PayPal created it, could never be sent, so skipping deletes it. Skip moves the item from `PENDING` to `REJECTED` with one conditional update. If an approval or a **Send** claimed it after Skip read it, nothing changes, Skip answers `409`, and the item ends as that approval leaves it. Cancelling the schedule cancels everything upcoming or pending, and any item an approval is sending, and leaves sent invoices alone. Each item is cancelled with one conditional update on the status Cancel read, so an item an approval marks sent meanwhile stays sent. It deletes a pending item's linked draft that PayPal never took. A draft of an item an approval is sending is left to that approval, which deletes it if PayPal refuses it. Approval is refused for an item whose schedule is not active, and the pending list shows only active schedules' items. If the schedule is cancelled while an approval waits on PayPal and PayPal fails, the item stays cancelled: the failure puts an item back to pending only if it is still being sent. A draft PayPal never took is deleted. A draft PayPal holds but did not send stays in the invoice list, where you can send it or leave it. If PayPal succeeds, the item is marked sent, since the client has the invoice. A schedule stays active, and can be cancelled, until its last invoice is approved or skipped.

The form offers an instalment plan only when the invoice scores High. The server checks the same rule. A plan sends nothing until the user approves its first instalment.
