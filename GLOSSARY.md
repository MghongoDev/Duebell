# Duebell

Duebell is an invoice-to-cash agent for a single freelancer. It turns a plain-English job description into a PayPal invoice, scores late-payment risk, and drafts reminders that a human approves.

## Language

**Client**:
A person or business that owes money on an invoice.
_Avoid_: Customer, buyer, account

**Invoice**:
A request for payment sent to a client. It carries an amount, a currency, payment terms, a due date, and a status.
_Avoid_: Bill, payment request

**Proposal**:
A structured invoice draft parsed from free text but not yet confirmed. It is not an invoice until a human confirms it.
_Avoid_: Draft invoice, parse result

**Payment terms**:
The number of days a client has to pay, counted from the issue date.
_Avoid_: Net terms, credit period

**Due date**:
The date on which an invoice becomes overdue.
_Avoid_: Deadline

**Risk score**:
A number from 0 to 1 that estimates how likely an invoice is to be paid late, based on the client's payment history and the invoice facts.
_Avoid_: Credit score, probability

**Risk level**:
The Low, Medium, or High band assigned from a risk score.
_Avoid_: Rating, grade

**Limited-data score**:
A risk result for a client with too little history to score with confidence. It is labeled as limited rather than presented as a firm score.
_Avoid_: Cold-start score, default score

**Reminder**:
A message that asks a client to pay an unpaid invoice. Duebell drafts it; a human approves it before PayPal sends it.
_Avoid_: Nudge, follow-up, chase

**Approval**:
A human decision that lets a drafted reminder leave the app. It is the only path to a PayPal send.
_Avoid_: Confirmation, sign-off

**Event**:
A message from PayPal about an invoice, such as sent, paid, cancelled, or refunded. Duebell stores each event once by its PayPal event ID.
_Avoid_: Notification, callback

**Gateway**:
The boundary that talks to PayPal. A mock gateway and a sandbox gateway implement the same interface.
_Avoid_: Client, adapter, connector

**Mock mode**:
A runtime mode that uses in-memory adapters for PayPal and the AI service, so the app runs with no credentials.
_Avoid_: Demo mode, fake mode

**Sandbox mode**:
A runtime mode that calls the real PayPal sandbox REST API. It moves no real money.
_Avoid_: Live mode, production mode

**Rule baseline**:
The shipped risk scorer. It combines named features with fixed weights and returns reasons a human can check.
_Avoid_: Heuristic, fallback model
