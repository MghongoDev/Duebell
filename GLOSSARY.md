# Duebell

Duebell is an invoice-to-cash agent for a single freelancer. It turns a plain-English job description into a PayPal invoice, scores late-payment risk, and drafts reminders that a human approves.

## Language

**User**:
A person who signs in to Duebell. Every client, invoice, reminder, schedule and event belongs to one user, and no user sees another's.
_Avoid_: Account holder, operator (in code), tenant

**Client**:
A person or business that owes money on an invoice.
_Avoid_: Customer, buyer, account

**Invoice**:
A request for payment sent to a client. It carries an amount, a currency, payment terms, a due date, and a status.
_Avoid_: Bill, payment request

**Minor unit**:
The smallest unit of a currency that an invoice can carry: a cent for USD, one yen for JPY. Duebell stores every amount as a whole number of minor units, in the `amount_cents` fields.
_Avoid_: Subunit, decimal amount

**Proposal**:
A structured invoice draft parsed from free text but not yet confirmed. It is not an invoice until a human confirms it.
_Avoid_: Draft invoice, parse result

**AI draft**:
The model's JSON answer to a description. It is untrusted until it passes the field allowlist and proposal validation.
_Avoid_: AI invoice, model output

**Guardrail**:
The fixed checks that reject unknown fields and invalid values before a proposal reaches the form.
_Avoid_: Safety layer, filter

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

**Known outcome**:
An invoice that has been paid, or is unpaid at least one full day past its due date. Only these count toward a client's history in the risk score and the risk comparison. Drafts and cancelled invoices never count.
_Avoid_: Settled invoice, closed invoice

**Reminder**:
A message that asks a client to pay an unpaid invoice. Duebell drafts it; a human approves it and picks the channel: PayPal, email, or both.
_Avoid_: Nudge, follow-up, chase

**Stage**:
Where an invoice stood when its reminder was drafted: before due, or overdue once it is a full day past its due date. An invoice gets at most one sent reminder in each stage. Stage is separate from tone: a High-risk invoice's before-due reminder is already firm.
_Avoid_: Phase, round, reminder level

**Scan**:
A pass that finds eligible invoices and creates pending reminder drafts. It never sends.
_Avoid_: Batch, job, run

**Approval**:
A human decision that lets a drafted reminder leave the app. It is the only path to a PayPal or email send.
_Avoid_: Confirmation, sign-off

**Editing**:
A human change to the text of a pending reminder. The edited text is what PayPal sends after approval.
_Avoid_: Rewrite, tweak

**Rejection**:
A human decision to drop a drafted reminder without sending it. A rejected draft can be drafted again on a later scan.
_Avoid_: Dismissal, deletion

**Event**:
A message from PayPal about an invoice, such as sent, paid, cancelled, or refunded. Duebell stores each event once by its PayPal event ID.
_Avoid_: Notification, callback

**Idempotency**:
The property that replaying the same PayPal event changes nothing and returns a duplicate result.
_Avoid_: Deduplication, replay guard

**Gateway**:
The boundary that talks to PayPal. The mock, sandbox and toolkit gateways implement the same interface.
_Avoid_: Client, adapter, connector

**Schedule**:
A rule that produces future invoices for one client: a repeat (weekly or monthly, with or without an end) or an instalment plan. Cancelling it stops future invoices and leaves sent ones alone.
_Avoid_: Subscription, series

**Instalment plan**:
A schedule that splits one amount into two to six invoices whose amounts sum exactly to the total. The form offers it when the client scores High risk.
_Avoid_: Payment plan (in code), partial payment

**Scheduled invoice**:
One future invoice from a schedule. The scan moves it from upcoming to pending; only an approval creates and sends it through PayPal.
_Avoid_: Queued invoice

**Channel**:
The route an approved reminder takes to the client: PayPal, email, or both. A reminder records each channel once it succeeds, so a retry never sends through the same channel twice.
_Avoid_: Medium, transport

**Mock mode**:
A runtime mode that uses the in-memory PayPal gateway, the narrow demo parser and the in-memory mailer, so the app runs with no credentials. The manual form still works.
_Avoid_: Demo mode, fake mode

**Live mode**:
An AI runtime mode that calls a real model endpoint. It is separate from PayPal's sandbox mode.
_Avoid_: Real mode, production mode

**Eval**:
A repeatable check of parse accuracy, reminder text, or webhook replay. Evals report results; they do not replace tests.
_Avoid_: Benchmark, test suite

**Sandbox mode**:
A runtime mode that calls the real PayPal sandbox REST API. It moves no real money.
_Avoid_: Live mode, production mode

**Rule baseline**:
The shipped risk scorer. It combines named features with fixed weights and returns reasons a human can check.
_Avoid_: Heuristic, fallback model

**Toolkit mode**:
A runtime mode that sends, reminds, records payments and reads status through PayPal's Agent Toolkit. Creating invoices and checking webhooks stay on the sandbox REST calls.
_Avoid_: Agent mode, AI mode

**Risk comparison**:
A logistic regression refit on one user's invoices with known outcomes, scored on held-out invoices beside the rule baseline. It informs; it never replaces the rule baseline.
_Avoid_: Model, retrained score

**Scope**:
Whose records a query may see: one user's ID, or every user for the background scheduler. Every query must state one.
_Avoid_: Tenant, filter

**Demo user**:
The account `demo@duebell.test` that the seed creates and gives the demo data to.
_Avoid_: Admin, test user
