"""Invoice services for Duebell (issues #9, #11, #23, #24, #46, #74, #78, #79). See ADRs 0005 and 0008.

Every read takes an ownership scope (app.ownership); creation records an owner.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Mapping
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, update

from app.config import Settings
from app.currency import format_amount
from app.models import (
    Client,
    Event,
    Invoice,
    InvoiceStatus,
    Reminder,
    ReminderStatus,
    as_utc,
    seed_invoice_counter,
    utcnow,
)
from app.ownership import Scope, get_owned, owned
from app.paypal import WEBHOOKS_OFF, PayPalError, PayPalGateway, PayPalTimeout
from app.risk import HistoryInvoice, RiskAssessment, score_invoice
from app.validation import InvoiceProposal, proposal_to_cents

# PayPal names several ways to be paid, refunded or cancelled. Duebell stores
# each as PayPal names it and treats it as its plain form (issue #112).
PAID_STATUSES = {InvoiceStatus.PAID, InvoiceStatus.MARKED_AS_PAID, InvoiceStatus.PAID_EXTERNAL}
REFUNDED_STATUSES = {InvoiceStatus.REFUNDED, InvoiceStatus.MARKED_AS_REFUNDED, InvoiceStatus.REFUNDED_EXTERNAL}
CANCELLED_STATUSES = {InvoiceStatus.CANCELLED, InvoiceStatus.AUTO_CANCELLED}
CLOSED_STATUSES = PAID_STATUSES | REFUNDED_STATUSES | CANCELLED_STATUSES
KNOWN_STATUSES = set(InvoiceStatus)
# PayPal's statuses for a payment pending review, a part payment and a part
# refund. Each leaves the invoice open (issue #112). Duebell keeps no paid
# amount, so each counts in full.
PARTIAL_STATUSES = {InvoiceStatus.PAYMENT_PENDING, InvoiceStatus.PARTIALLY_PAID, InvoiceStatus.PARTIALLY_REFUNDED}
# Sent, not closed, and owed: the outstanding total and the reminder scan
# count these (issue #112).
OWED_STATUSES = {InvoiceStatus.SENT, InvoiceStatus.OVERDUE} | PARTIAL_STATUSES
# The statuses of an invoice PayPal has sent. A repeat held on a draft starts,
# and a scheduled item linked to it counts as sent, once the draft reaches one
# of them (issues #78, #79).
SENT_STATUSES = OWED_STATUSES | PAID_STATUSES
# Never part of a client's payment history (issue #46).
NOT_HISTORY = {InvoiceStatus.DRAFT} | CANCELLED_STATUSES

EVENT_STATUS = {
    "INVOICING.INVOICE.SENT": InvoiceStatus.SENT,
    "INVOICING.INVOICE.PAID": InvoiceStatus.PAID,
    "INVOICING.INVOICE.CANCELLED": InvoiceStatus.CANCELLED,
    "INVOICING.INVOICE.REFUNDED": InvoiceStatus.REFUNDED,
}


def money(cents: int, currency: str) -> str:
    """Format minor units for display, without a currency code: "1,200.00" USD, "1,000" JPY (issue #44)."""
    return format_amount(cents, currency)


def find_client(session: Session, email: str, user_id: int | None) -> Client | None:
    """Look up one user's client by email without creating anything."""
    email = email.strip().lower()
    if not email:
        return None
    return session.exec(select(Client).where(Client.email == email, Client.user_id == user_id)).first()


def preview_risk(
    session: Session, user_id: int, *, email: str, amount_cents: int, terms_days: int, currency: str
) -> RiskAssessment:
    """Score an invoice that does not exist yet. Writes nothing."""
    client = find_client(session, email, user_id)
    now = utcnow()
    history = client_history(session, client.id, now) if client else []
    return score_invoice(amount_cents, terms_days, history, now=now, currency=currency.strip().upper())


def get_or_create_client(session: Session, name: str, email: str, user_id: int | None) -> Client:
    email = email.strip().lower()
    client = find_client(session, email, user_id)
    if client:
        if name.strip() and client.name != name.strip():
            client.name = name.strip()
            session.add(client)
        return client
    client = Client(name=name.strip(), email=email, user_id=user_id)
    session.add(client)
    session.commit()
    session.refresh(client)
    return client


NUMBER_ATTEMPTS = 20
SEND_UNCONFIRMED = (
    "PayPal did not confirm the send in time and may still send it. "
    "Duebell counts the invoice as sent so it never goes twice. Check PayPal."
)


def next_invoice_number(session: Session, offset: int = 0) -> str:
    """Take the next number from the counter, in the open transaction (issues #39, #114).

    The counter only goes up and stays above every invoice id, so a number is
    never handed out twice, even after its draft is deleted. The update takes
    SQLite's write lock, so two requests cannot read the same value. The unique
    index still settles a clash with a number added some other way.
    """
    connection = session.connection()
    bump = text(
        "UPDATE invoicenumbercounter "
        "SET last = MAX(last, (SELECT COALESCE(MAX(id), 0) FROM invoice)) + 1 + :offset WHERE id = 1"
    )
    if connection.execute(bump, {"offset": offset}).rowcount == 0:
        seed_invoice_counter(connection)
        connection.execute(bump, {"offset": offset})
    last = connection.execute(text("SELECT last FROM invoicenumbercounter WHERE id = 1")).scalar_one()
    return f"DUE-{utcnow():%Y%m%d}-{last:04d}"


def _insert_numbered(session: Session, invoice: Invoice) -> None:
    """Commit a new invoice with the next number, in the same transaction as the counter.

    If the number is already taken, the rollback undoes the counter too, so
    the next try skips one more number.
    """
    for offset in range(NUMBER_ATTEMPTS):
        invoice.invoice_number = next_invoice_number(session, offset)
        session.add(invoice)
        try:
            session.commit()
            return
        except IntegrityError:
            session.rollback()
    raise PayPalError("Could not give the invoice a unique number. Try again.")


def history_as_of(invoices: list[Invoice], as_of: datetime) -> list[HistoryInvoice]:
    """A client's invoices as they stood at `as_of`. Live scoring and the refit both use this (issue #46).

    Drafts were never sent and cancelled invoices were withdrawn, so neither
    is history. Invoice has no cancellation date, so an invoice cancelled
    after `as_of` is left out too. A payment after `as_of` had not happened
    yet. The risk rule then counts only invoices whose outcome is known at
    `as_of` (app.risk.outcome_known).
    """
    history = []
    for item in invoices:
        if item.status in NOT_HISTORY or as_utc(item.issued_at) >= as_of:
            continue
        paid = as_utc(item.paid_at)
        history.append(
            HistoryInvoice(
                amount_cents=item.amount_cents,
                due_at=as_utc(item.due_at),
                paid_at=paid if paid is not None and paid <= as_of else None,
                currency=item.currency,
            )
        )
    return history


def client_history(session: Session, client_id: int, now: datetime | None = None) -> list[HistoryInvoice]:
    invoices = session.exec(select(Invoice).where(Invoice.client_id == client_id)).all()
    return history_as_of(list(invoices), as_utc(now or utcnow()))


def draft_invoice(session: Session, proposal: InvoiceProposal, *, user_id: int | None) -> Invoice:
    """Save a numbered, risk-scored DRAFT and commit it. Calls nothing outside (issue #74).

    The caller can link the committed row before calling PayPal, as
    approve_scheduled does, so a retry finds the invoice it already made.
    """
    client = get_or_create_client(session, proposal.client_name, proposal.client_email, user_id)
    now = utcnow()
    currency = proposal.currency.strip().upper()
    amount_cents = proposal_to_cents(proposal.amount, currency)
    assessment = score_invoice(
        amount_cents,
        proposal.terms_days,
        client_history(session, client.id, now),
        now=now,
        currency=currency,
    )
    invoice = Invoice(
        user_id=user_id,
        client_id=client.id,
        invoice_number="",
        description=proposal.description.strip(),
        amount_cents=amount_cents,
        currency=currency,
        terms_days=proposal.terms_days,
        issued_at=now,
        due_at=now + timedelta(days=proposal.terms_days),
        status=InvoiceStatus.DRAFT,
        risk_score=assessment.score,
        risk_level=assessment.level,
        risk_reasons="\n".join(assessment.reasons),
        limited_data=assessment.limited_data,
        limited_data_reason=assessment.limited_data_reason,
    )
    _insert_numbered(session, invoice)
    session.refresh(invoice)
    return invoice


def publish_invoice(
    session: Session, invoice: Invoice, gateway: PayPalGateway, *, warnings: list[str] | None = None
) -> Invoice:
    """Create a saved DRAFT in PayPal, commit its PayPal ID, then send it (issue #74).

    The invoice takes its dates when PayPal creates it, so a draft saved before
    a restart does not go out with old dates. The send starts only once the
    PayPal ID is committed. Raises PayPalError if PayPal refuses to create it,
    leaving the row as it was, and InvoiceNotSent if the send fails.
    """
    if invoice.status != InvoiceStatus.DRAFT or invoice.paypal_invoice_id:
        raise ValueError("Only a saved draft PayPal does not hold yet can be created there.")
    client = session.get(Client, invoice.client_id)
    now = utcnow()
    due_at = now + timedelta(days=invoice.terms_days)
    paypal_id = gateway.create_invoice(
        invoice_number=invoice.invoice_number,
        amount_cents=invoice.amount_cents,
        currency=invoice.currency,
        description=invoice.description,
        terms_days=invoice.terms_days,
        client_name=client.name,
        client_email=client.email,
        issued_at=now,
        due_at=due_at,
    )
    invoice.issued_at = now
    invoice.due_at = due_at
    invoice.paypal_invoice_id = paypal_id
    session.add(invoice)
    session.commit()
    return send_unsent_invoice(session, invoice, gateway, warnings=warnings)


def create_invoice(
    session: Session,
    proposal: InvoiceProposal,
    settings: Settings,
    gateway: PayPalGateway,
    *,
    user_id: int | None,
    warnings: list[str] | None = None,
) -> Invoice:
    """Save the draft, then create and send it in PayPal. The form's path.

    If PayPal refuses to create it, the draft is deleted and the error raised.
    """
    invoice = draft_invoice(session, proposal, user_id=user_id)
    try:
        return publish_invoice(session, invoice, gateway, warnings=warnings)
    except InvoiceNotSent:
        raise
    except PayPalError:
        session.delete(invoice)
        session.commit()
        raise


class InvoiceNotSent(PayPalError):
    """PayPal holds the invoice, but sending it failed.

    The caller keeps `invoice` and retries with send_unsent_invoice, so a retry
    never creates a second PayPal invoice.
    """

    def __init__(self, message: str, invoice: Invoice) -> None:
        super().__init__(message)
        self.invoice = invoice


def send_unsent_invoice(
    session: Session, invoice: Invoice, gateway: PayPalGateway, *, warnings: list[str] | None = None
) -> Invoice:
    """Send an invoice PayPal holds but has not sent. Raises InvoiceNotSent on failure.

    A timeout counts as sent, with a warning: PayPal may still act, and sending
    again could bill the client twice (issue #39, as reminders do under ADR 0003).
    A status a webhook stored during the call stays if SENT may not replace
    it, and then no warning is given (issue #115).
    """
    if invoice.status != InvoiceStatus.DRAFT or not invoice.paypal_invoice_id:
        raise ValueError("Only an invoice PayPal holds unsent can be sent this way.")
    timed_out = False
    try:
        gateway.send_invoice(invoice.paypal_invoice_id)
    except PayPalTimeout:
        timed_out = True
    except PayPalError as exc:
        session.refresh(invoice)
        raise InvoiceNotSent(str(exc), invoice) from exc
    # A webhook may have paid or cancelled the invoice during the call; that
    # status stays, with no warning (issue #115).
    if move_status(session, invoice, InvoiceStatus.SENT) and timed_out and warnings is not None:
        warnings.append(SEND_UNCONFIRMED)
    session.commit()
    session.refresh(invoice)
    return invoice


def list_invoices(session: Session, scope: Scope) -> list[Invoice]:
    query = owned(select(Invoice), Invoice, scope).order_by(Invoice.created_at.desc())
    return list(session.exec(query).all())


def get_invoice(session: Session, invoice_id: int, user_id: int) -> Invoice | None:
    return get_owned(session, Invoice, invoice_id, user_id)


def client_map(session: Session, scope: Scope) -> dict[int, Client]:
    return {client.id: client for client in session.exec(owned(select(Client), Client, scope)).all()}


def is_overdue(invoice: Invoice, now: datetime) -> bool:
    """True once the UTC calendar day of the invoice's due_at has ended (issues #80, #119).

    PayPal gets only a due date, `due_at`'s UTC date (see `payment_term`), so
    the client sees a due day, not a time. The invoice stays due all that day
    and is overdue from the next UTC midnight. The reminder scan, its stage
    and wording, and the dashboard's overdue flag all use this one test.
    """
    return as_utc(invoice.due_at).astimezone(timezone.utc).date() < as_utc(now).astimezone(timezone.utc).date()


def invoice_rows(session: Session, scope: Scope, now: datetime | None = None) -> list[dict]:
    clients = client_map(session, scope)
    now = now or utcnow()
    rows = []
    for invoice in list_invoices(session, scope):
        client = clients.get(invoice.client_id)
        closed = invoice.status in CLOSED_STATUSES
        # A draft was never sent, so the client owes nothing yet (issue #116).
        overdue = not closed and invoice.status != InvoiceStatus.DRAFT and is_overdue(invoice, now)
        reasons = [line for line in invoice.risk_reasons.splitlines() if line.strip()]
        rows.append(
            {
                "id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "client_name": client.name if client else "",
                "client_email": client.email if client else "",
                "description": invoice.description,
                "amount_cents": invoice.amount_cents,
                "amount_display": money(invoice.amount_cents, invoice.currency),
                "currency": invoice.currency,
                "terms_days": invoice.terms_days,
                "issued_at": invoice.issued_at.isoformat(timespec="minutes"),
                "due_at": invoice.due_at.isoformat(timespec="minutes"),
                "due_display": invoice.due_at.strftime("%d %b %Y"),
                "paid_at": invoice.paid_at.isoformat(timespec="minutes") if invoice.paid_at else None,
                "status": invoice.status,
                "overdue": overdue,
                "closed": closed,
                "risk_score": invoice.risk_score,
                "risk_level": invoice.risk_level,
                "risk_reasons": reasons,
                "limited_data": bool(invoice.limited_data),
                "limited_data_reason": invoice.limited_data_reason if invoice.limited_data else None,
                "paypal_invoice_id": invoice.paypal_invoice_id,
            }
        )
    return rows


def dashboard_totals(rows: list[dict]) -> dict:
    # Outstanding money owed = invoices that have left draft and are not yet
    # settled. A DRAFT has never been sent, so it is not a debt to collect.
    # A partly paid or partly refunded invoice counts in full (issue #112).
    # Amounts in different currencies are never added together.
    outstanding = _amount_by_currency(
        row
        for row in rows
        if row["status"] in OWED_STATUSES and row.get("paypal_invoice_id")
    )
    paid = _amount_by_currency(row for row in rows if row["status"] in PAID_STATUSES)
    overdue = sum(1 for row in rows if row["overdue"])
    return {
        "outstanding": outstanding,
        "paid": paid,
        "outstanding_display": _money_by_currency(outstanding),
        "overdue_count": overdue,
        "overdue_by_currency": _count_by_currency(row for row in rows if row["overdue"]),
        "paid_display": _money_by_currency(paid),
        "invoice_count": len(rows),
    }


def _tally_by_currency(rows, weigh) -> dict[str, int]:
    """Add up weigh(row) per currency, sorted by currency code."""
    totals: dict[str, int] = {}
    for row in rows:
        totals[row["currency"]] = totals.get(row["currency"], 0) + weigh(row)
    return dict(sorted(totals.items()))


def _amount_by_currency(rows) -> dict[str, int]:
    """Total amount_cents per currency. Amounts in different currencies never add up."""
    return _tally_by_currency(rows, lambda row: row["amount_cents"])


def _count_by_currency(rows) -> dict[str, int]:
    """How many rows there are in each currency."""
    return _tally_by_currency(rows, lambda row: 1)


def _money_by_currency(totals: dict[str, int]) -> str:
    if not totals:
        return "0.00"
    return " · ".join(f"{money(cents, code)} {code}" for code, cents in totals.items())


def new_invoice_number() -> str:
    return f"DUE-{utcnow():%Y%m%d}-{uuid4().hex[:6].upper()}"


def find_by_paypal_id(session: Session, paypal_invoice_id: str) -> Invoice | None:
    return session.exec(
        select(Invoice).where(Invoice.paypal_invoice_id == paypal_invoice_id)
    ).first()


def may_replace(current: str, new: str) -> bool:
    """Whether status new may replace status current (issue #37).

    A status only moves forward. It never leaves a closed status, except that
    a paid invoice may become partly or fully refunded, and it never goes back
    to DRAFT. A status Duebell does not know, such as the gateway's UNKNOWN,
    never applies.

    Partial statuses keep their order (issue #112). Money in moves
    SENT -> PARTIALLY_PAID -> PAID, so a partly paid invoice never goes back
    to SENT or OVERDUE. PAYMENT_PENDING may sit before or after a part
    payment, and may go back to SENT if the payment fails. Money out moves
    PAID -> PARTIALLY_REFUNDED -> REFUNDED, so a partly refunded invoice may
    only become refunded. Each paid and refunded status stands for all of
    PayPal's names for it.
    """
    if new not in KNOWN_STATUSES:
        return False
    if new == current:
        return True
    if current in CLOSED_STATUSES:
        return current in PAID_STATUSES and new in REFUNDED_STATUSES | {InvoiceStatus.PARTIALLY_REFUNDED}
    if current == InvoiceStatus.PARTIALLY_REFUNDED:
        return new in REFUNDED_STATUSES
    if current == InvoiceStatus.PARTIALLY_PAID:
        return new not in {InvoiceStatus.DRAFT, InvoiceStatus.SENT, InvoiceStatus.OVERDUE}
    return new != InvoiceStatus.DRAFT


def apply_status(invoice: Invoice, status: str) -> None:
    invoice.status = status
    if status in PAID_STATUSES and invoice.paid_at is None:
        invoice.paid_at = utcnow()
    if status in CLOSED_STATUSES - PAID_STATUSES:
        # A cancelled or refunded invoice never starts the repeat it held (issue #78).
        invoice.repeat_unit = None
        invoice.repeat_cycles = None


def move_status(session: Session, invoice: Invoice, status: str) -> bool:
    """Apply status if it may replace the one stored now. The caller commits (issue #115).

    A PayPal call can take seconds, and a webhook may commit a newer status
    meanwhile, so the status read before the call may be out of date. The
    no-op update takes SQLite's write lock first, then the invoice is read
    again, so may_replace sees the stored status and nothing changes it before
    the caller commits. Returns whether the status changed.
    """
    session.exec(update(Invoice).where(Invoice.id == invoice.id).values(status=Invoice.status))
    session.refresh(invoice)
    if not may_replace(invoice.status, status):
        return False
    changed = invoice.status != status
    apply_status(invoice, status)
    session.add(invoice)
    return changed


def follow_sent_invoice(session: Session, invoice: Invoice) -> None:
    """Follow up this invoice if PayPal has sent it (issues #78, #79).

    Called after every status change commits, whatever moved the invoice:
    the Send button, a webhook or a Refresh. app.schedules.follow_sent starts
    the repeat held on the invoice and marks the scheduled item linked to it
    SENT. Each step is one conditional update, so a race does it once, and a
    step a crash missed runs on the next webhook or Refresh.

    app.schedules imports this module, so the import sits here to avoid a
    cycle (issue #103).
    """
    if invoice.status in SENT_STATUSES:
        from app.schedules import follow_sent

        follow_sent(session, invoice)


def reject_drafts_if_closed(session: Session, invoice: Invoice) -> int:
    """Reject pending reminder drafts once their invoice is paid, cancelled or refunded.

    Nothing should reach a client about a closed invoice (issue #36). The caller commits.
    """
    if invoice.status not in CLOSED_STATUSES:
        return 0
    drafts = session.exec(
        select(Reminder).where(Reminder.invoice_id == invoice.id, Reminder.status == ReminderStatus.PENDING)
    ).all()
    for draft in drafts:
        draft.status = ReminderStatus.REJECTED
        session.add(draft)
    return len(drafts)


def refresh_invoice(session: Session, invoice: Invoice, gateway) -> Invoice:
    """Read the status from PayPal and apply it if it still moves forward (issues #37, #115)."""
    if not invoice.paypal_invoice_id:
        return invoice
    status = gateway.get_invoice(invoice.paypal_invoice_id)
    move_status(session, invoice, status)
    reject_drafts_if_closed(session, invoice)
    session.commit()
    session.refresh(invoice)
    follow_sent_invoice(session, invoice)
    return invoice


# The event type of the feed line a simulated payment leaves (issue #125).
# PayPal's own event types read INVOICING.INVOICE.*, so the two never mix.
SIMULATED_PAYMENT = "Simulated payment"


def simulate_payment(session: Session, invoice: Invoice, gateway) -> Invoice:
    """Record a payment in PayPal, then mark the invoice PAID if it still may be (issue #115).

    The payment also adds a line to the owner's activity feed: a local Event
    whose ID starts with SIMULATED-, not a PayPal webhook event (issue #125).
    """
    if not invoice.paypal_invoice_id:
        raise ValueError("Invoice has no PayPal id yet.")
    if invoice.status in CLOSED_STATUSES:
        raise ValueError("This invoice is already closed.")
    gateway.record_payment(invoice.paypal_invoice_id, invoice.amount_cents, invoice.currency)
    move_status(session, invoice, InvoiceStatus.PAID)
    session.add(
        Event(
            paypal_event_id=f"SIMULATED-{invoice.invoice_number}-{uuid4().hex[:8].upper()}",
            user_id=invoice.user_id,
            event_type=SIMULATED_PAYMENT,
        )
    )
    reject_drafts_if_closed(session, invoice)
    session.commit()
    session.refresh(invoice)
    follow_sent_invoice(session, invoice)
    return invoice


def handle_webhook(session: Session, gateway, headers: Mapping[str, str], body: str) -> dict:
    if not gateway.accepts_webhooks:
        # Mock mode with MOCK_WEBHOOKS off: refuse and store nothing (issue #38).
        return {"ok": False, "error": WEBHOOKS_OFF}
    if not gateway.verify_webhook(headers, body):
        return {"ok": False, "error": "invalid signature"}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid json"}

    event_id = payload.get("id") or ""
    event_type = payload.get("event_type") or ""
    if not event_id:
        return {"ok": False, "error": "missing event id"}

    existing = session.get(Event, event_id)
    if existing:
        return {"ok": True, "duplicate": True}

    # Store the event before reading the invoice (issue #48). The insert takes
    # SQLite's write lock, so a copy of this event arriving at the same moment
    # waits, then fails on the primary key and counts as a duplicate. Another
    # event for the same invoice also waits, so may_replace below always sees
    # the status the last event left.
    event = Event(paypal_event_id=event_id, event_type=event_type, payload=body[:20000])
    session.add(event)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return {"ok": True, "duplicate": True}

    resource = payload.get("resource") or {}
    carried = resource if resource.get("id") else resource.get("invoice") or {}
    paypal_invoice_id = carried.get("id") or ""
    # PAID also covers a part payment or a pending one, and REFUNDED a part
    # refund, so the invoice's own status wins. The event type only names the
    # status when the invoice carries none (issue #112).
    status = carried.get("status") or EVENT_STATUS.get(event_type)
    applied = False
    owner_id = None
    invoice = find_by_paypal_id(session, paypal_invoice_id) if paypal_invoice_id else None
    if invoice:
        owner_id = invoice.user_id
    if invoice and status and may_replace(invoice.status, status):
        apply_status(invoice, status)
        session.add(invoice)
        reject_drafts_if_closed(session, invoice)
        applied = True

    event.user_id = owner_id
    session.add(event)
    session.commit()
    if invoice:
        follow_sent_invoice(session, invoice)
    return {"ok": True, "applied": applied, "status": status}


def list_events(session: Session, scope: Scope, limit: int = 20) -> list[Event]:
    query = owned(select(Event), Event, scope).order_by(Event.received_at.desc()).limit(limit)
    return list(session.exec(query).all())
