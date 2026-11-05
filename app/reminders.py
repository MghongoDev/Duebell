"""Reminder drafting, editing, approval, and rejection (issue #15).

Scanning only creates drafts. A human must edit or approve a draft before
Duebell asks PayPal to send a reminder. The draft uses only facts already on
the invoice. See ADR 0003.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session, select

from app.config import Settings
from app.models import Client, Invoice, Reminder, utcnow

REMINDABLE_STATUSES = {"SENT", "OVERDUE"}
BLOCKING_REMINDER_STATUSES = {"PENDING", "SENT"}
MAX_DRAFT_CHARS = 2_000
CHANNELS = ("paypal", "email")


def money(cents: int) -> str:
    return f"{cents / 100:,.2f}"


def reminder_text(
    invoice: Invoice,
    client: Client,
    settings: Settings,
    now: datetime | None = None,
) -> tuple[str, str]:
    now = now or utcnow()
    overdue_days = max(0, (now - invoice.due_at).days)
    tone = "firm" if overdue_days > 14 or invoice.risk_level == "HIGH" else "friendly"
    signature = settings.paypal_invoicer_email or "Duebell"

    if overdue_days > 0:
        opening = (
            f"Invoice {invoice.invoice_number} for {money(invoice.amount_cents)} "
            f"{invoice.currency} was due on {invoice.due_at:%d %b %Y} and is still open."
        )
    else:
        opening = (
            f"A friendly note that invoice {invoice.invoice_number} for "
            f"{money(invoice.amount_cents)} {invoice.currency} is due on "
            f"{invoice.due_at:%d %b %Y}."
        )

    if tone == "firm":
        ask = (
            "Please confirm the payment date this week. If the invoice is wrong or "
            "the terms need to change, tell us now so we can fix it."
        )
    else:
        ask = "Could you confirm the payment date? If anything looks wrong, reply and we will sort it out."

    text = (
        f"Hi {client.name},\n\n{opening}\n\n{ask}\n\n"
        f"Thanks,\n{signature}"
    )
    return text, tone


def is_due_for_reminder(invoice: Invoice, settings: Settings, now: datetime | None = None) -> bool:
    if invoice.status not in REMINDABLE_STATUSES or invoice.paid_at is not None:
        return False
    now = now or utcnow()
    overdue = invoice.due_at < now
    due_soon = invoice.due_at <= now + timedelta(days=settings.reminder_window_days)
    if not due_soon:
        return False
    if overdue:
        return True
    return invoice.risk_level in {"MEDIUM", "HIGH"}


def _blocked_invoice_ids(session: Session) -> set[int]:
    reminders = session.exec(
        select(Reminder).where(Reminder.status.in_(BLOCKING_REMINDER_STATUSES))
    ).all()
    return {reminder.invoice_id for reminder in reminders}


def draft_reminders(
    session: Session,
    settings: Settings,
    now: datetime | None = None,
) -> list[Reminder]:
    blocked = _blocked_invoice_ids(session)
    created: list[Reminder] = []
    for invoice in session.exec(select(Invoice).where(Invoice.status.in_(REMINDABLE_STATUSES))).all():
        if invoice.id in blocked or not is_due_for_reminder(invoice, settings, now):
            continue
        # A draft is only useful if approving it can reach PayPal.
        if not invoice.paypal_invoice_id:
            continue
        client = session.get(Client, invoice.client_id)
        if client is None:
            continue
        text, tone = reminder_text(invoice, client, settings, now)
        reminder = Reminder(invoice_id=invoice.id, draft_text=text, tone=tone, status="PENDING")
        session.add(reminder)
        created.append(reminder)
    if created:
        session.commit()
        for reminder in created:
            session.refresh(reminder)
    return created


def parse_channels(value) -> tuple[str, ...]:
    """Normalise a channel choice: "paypal", "email", "both", or a list of them."""
    if value is None or value == "":
        return ("paypal",)
    items = [value] if isinstance(value, str) else list(value)
    chosen: list[str] = []
    for item in items:
        for part in str(item).split(","):
            part = part.strip().lower()
            names = CHANNELS if part == "both" else (part,)
            for name in names:
                if name not in CHANNELS:
                    raise ValueError(f"Unknown reminder channel: {part}.")
                if name not in chosen:
                    chosen.append(name)
    if not chosen:
        raise ValueError("Pick at least one channel.")
    return tuple(name for name in CHANNELS if name in chosen)


def sent_channels(reminder: Reminder) -> list[str]:
    return [name for name in (reminder.sent_channels or "").split(",") if name]


def send_reminder(
    session: Session,
    reminder: Reminder,
    gateway,
    mailer=None,
    channels=("paypal",),
) -> Reminder:
    """Send an approved draft. This is the only path out of the app (ADR 0003).

    Each channel is recorded as soon as it succeeds. If a later channel fails,
    the draft stays pending and a retry skips the channels already done, so
    PayPal never sends the same reminder twice.
    """
    if reminder.status != "PENDING":
        raise ValueError("Only a pending draft can be sent.")
    channels = parse_channels(channels)
    invoice = session.get(Invoice, reminder.invoice_id)
    if invoice is None:
        raise ValueError("This reminder has no invoice.")
    if "paypal" in channels and not invoice.paypal_invoice_id:
        raise ValueError("This invoice has no PayPal id yet.")
    if "email" in channels and mailer is None:
        raise ValueError("Email is not available.")

    subject = f"Reminder: invoice {invoice.invoice_number}"
    done = sent_channels(reminder)
    for channel in channels:
        if channel in done:
            continue
        if channel == "paypal":
            gateway.send_reminder(invoice.paypal_invoice_id, subject, reminder.draft_text)
        else:
            client = session.get(Client, invoice.client_id)
            if client is None:
                raise ValueError("This invoice has no client email.")
            mailer.send(client.email, subject, reminder.draft_text)
        done.append(channel)
        reminder.sent_channels = ",".join(name for name in CHANNELS if name in done)
        session.add(reminder)
        session.commit()

    reminder.status = "SENT"
    reminder.sent_at = utcnow()
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return reminder


def edit_reminder(session: Session, reminder: Reminder, text: str) -> Reminder:
    if reminder.status != "PENDING":
        raise ValueError("Only a pending draft can be edited.")
    text = text.strip()
    if not text:
        raise ValueError("The draft cannot be empty.")
    if len(text) > MAX_DRAFT_CHARS:
        raise ValueError(f"Keep the draft under {MAX_DRAFT_CHARS:,} characters.")
    reminder.draft_text = text
    reminder.tone = "custom"
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return reminder


def reject_reminder(session: Session, reminder: Reminder) -> Reminder:
    if reminder.status != "PENDING":
        raise ValueError("Only a pending draft can be rejected.")
    reminder.status = "REJECTED"
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return reminder


def reminder_rows(session: Session) -> list[dict]:
    reminders = session.exec(select(Reminder).order_by(Reminder.created_at.desc())).all()
    invoices = {invoice.id: invoice for invoice in session.exec(select(Invoice)).all()}
    clients = {client.id: client for client in session.exec(select(Client)).all()}
    rows = []
    for reminder in reminders:
        invoice = invoices.get(reminder.invoice_id)
        client = clients.get(invoice.client_id) if invoice else None
        rows.append(
            {
                "id": reminder.id,
                "invoice_id": reminder.invoice_id,
                "invoice_number": invoice.invoice_number if invoice else "",
                "client_name": client.name if client else "",
                "amount_display": money(invoice.amount_cents) if invoice else "",
                "currency": invoice.currency if invoice else "",
                "due_display": invoice.due_at.strftime("%d %b %Y") if invoice else "",
                "draft_text": reminder.draft_text,
                "tone": reminder.tone,
                "status": reminder.status,
                "created_display": reminder.created_at.strftime("%d %b %H:%M"),
                "sent_at": reminder.sent_at.isoformat(timespec="minutes") if reminder.sent_at else None,
                "sent_at_display": reminder.sent_at.strftime("%d %b %H:%M") if reminder.sent_at else "",
                "sent_channels": sent_channels(reminder),
                "has_paypal_id": bool(invoice and invoice.paypal_invoice_id),
            }
        )
    return rows
