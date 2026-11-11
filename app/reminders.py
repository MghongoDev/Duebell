"""Reminder drafting, editing, approval, and rejection (issues #15, #24, #26, #40, #42; ADR 0003).

Scanning only creates drafts. A human must edit or approve a draft before
Duebell asks PayPal to send a reminder. The draft uses only facts already on
the invoice. See ADR 0003.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, or_, select, update

from app.config import Settings
from app.mailer import MailError, Mailer, MailMaybeSent
from app.models import Client, Invoice, Reminder, ReminderStage, ReminderStatus, utcnow
from app.ownership import Scope, owned
from app.paypal import PayPalError, PayPalGateway, PayPalTimeout
from app.risk import RiskLevel
from app.services import CLOSED_STATUSES, OWED_STATUSES, is_overdue, money, reject_drafts_if_closed

# Every owed status, partial ones included (issue #112).
REMINDABLE_STATUSES = OWED_STATUSES
MAX_DRAFT_CHARS = 2_000


class Channel(StrEnum):
    """A route an approved reminder takes to the client (issue #26)."""

    PAYPAL = "paypal"
    EMAIL = "email"

    @property
    def label(self) -> str:
        return "PayPal" if self is Channel.PAYPAL else "email"


CHANNELS = tuple(Channel)


def reminder_stage(invoice: Invoice, now: datetime) -> ReminderStage:
    """The stage a reminder drafted at `now` belongs to (issues #42, #80, #119).

    It matches the reminder's opening line: OVERDUE once the invoice's UTC
    due day has ended, BEFORE_DUE until then, the due day included (issue #119).
    """
    if is_overdue(invoice, now):
        return ReminderStage.OVERDUE
    return ReminderStage.BEFORE_DUE


def reminder_text(
    invoice: Invoice,
    client: Client,
    settings: Settings,
    now: datetime | None = None,
) -> tuple[str, str]:
    now = now or utcnow()
    overdue_days = max(0, (now - invoice.due_at).days)
    tone = "firm" if overdue_days > 14 or invoice.risk_level == RiskLevel.HIGH else "friendly"
    signature = settings.paypal_invoicer_email or "Duebell"

    if reminder_stage(invoice, now) is ReminderStage.OVERDUE:
        opening = (
            f"Invoice {invoice.invoice_number} for {money(invoice.amount_cents, invoice.currency)} "
            f"{invoice.currency} was due on {invoice.due_at:%d %b %Y} and is still open."
        )
    else:
        opening = (
            f"A friendly note that invoice {invoice.invoice_number} for "
            f"{money(invoice.amount_cents, invoice.currency)} {invoice.currency} is due on "
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
    overdue = is_overdue(invoice, now)
    due_soon = invoice.due_at <= now + timedelta(days=settings.reminder_window_days)
    if not due_soon:
        return False
    if overdue:
        return True
    return invoice.risk_level in {RiskLevel.MEDIUM, RiskLevel.HIGH}


# A draft waiting for approval, or one being sent, blocks any new draft (issues #42, #48).
OPEN_DRAFT_STATUSES = {ReminderStatus.PENDING, ReminderStatus.SENDING}


def _blocked_invoice_ids(session: Session) -> set[int]:
    """Invoices with a draft pending or being sent. It blocks any new draft (ADR 0003)."""
    reminders = session.exec(select(Reminder).where(Reminder.status.in_(OPEN_DRAFT_STATUSES))).all()
    return {reminder.invoice_id for reminder in reminders}


def _sent_stages(session: Session) -> set[tuple[int, ReminderStage]]:
    """(invoice, stage) pairs that already have a sent reminder (issues #42, #76).

    A reminder counts as sent once any channel delivered it, whatever its
    status: a draft PayPal sent and the user then rejected still reached the
    client. A rejected draft no channel delivered counts for nothing.

    A reminder from a build before issue #42 has no stage. It counts in the
    stage it would get if drafted at its created_at: overdue if that was after
    the invoice's UTC due day, before due otherwise (issues #80, #119).
    """
    rows = session.exec(
        select(Reminder, Invoice)
        .join(Invoice, Invoice.id == Reminder.invoice_id)
        .where(or_(Reminder.status == ReminderStatus.SENT, Reminder.sent_channels != ""))
    ).all()
    return {
        (reminder.invoice_id, ReminderStage(reminder.stage or reminder_stage(invoice, reminder.created_at)))
        for reminder, invoice in rows
    }


def draft_reminders(
    session: Session,
    settings: Settings,
    now: datetime | None = None,
    *,
    scope: Scope,
) -> list[Reminder]:
    """Draft one reminder for each invoice that needs one. Nothing is sent here.

    An invoice gets at most one sent reminder per stage: one before it is due
    and one once it is overdue. A reminder any channel delivered counts as
    sent, even if the user later rejected the rest of it (issue #76). A
    pending draft blocks any new draft; a rejected one no channel delivered
    blocks nothing (issue #42).

    Each draft is committed on its own. If another scan drafted one for the
    same invoice first, the database refuses a second pending draft and this
    scan skips that invoice (issue #40).
    """
    closed = owned(select(Invoice).where(Invoice.status.in_(CLOSED_STATUSES)), Invoice, scope)
    if sum(reject_drafts_if_closed(session, invoice) for invoice in session.exec(closed).all()):
        session.commit()
    now = now or utcnow()
    blocked = _blocked_invoice_ids(session)
    sent = _sent_stages(session)
    created: list[Reminder] = []
    query = owned(select(Invoice).where(Invoice.status.in_(REMINDABLE_STATUSES)), Invoice, scope)
    for invoice in session.exec(query).all():
        if invoice.id in blocked or not is_due_for_reminder(invoice, settings, now):
            continue
        stage = reminder_stage(invoice, now)
        if (invoice.id, stage) in sent:
            continue
        # A draft is only useful if approving it can reach PayPal.
        if not invoice.paypal_invoice_id:
            continue
        client = session.get(Client, invoice.client_id)
        if client is None:
            continue
        text, tone = reminder_text(invoice, client, settings, now)
        reminder = Reminder(
            user_id=invoice.user_id,
            invoice_id=invoice.id,
            draft_text=text,
            tone=tone,
            stage=stage,
            status=ReminderStatus.PENDING,
        )
        session.add(reminder)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            continue
        session.refresh(reminder)
        created.append(reminder)
    return created


def parse_channels(value: str | Iterable[str] | None) -> tuple[Channel, ...]:
    """Normalise a channel choice: "paypal", "email", "both", or a list of them."""
    if value is None or value == "":
        return (Channel.PAYPAL,)
    items = [value] if isinstance(value, str) else list(value)
    chosen: set[Channel] = set()
    for item in items:
        for part in str(item).split(","):
            part = part.strip().lower()
            if part == "both":
                chosen.update(CHANNELS)
                continue
            try:
                chosen.add(Channel(part))
            except ValueError:
                raise ValueError(f"Unknown reminder channel: {part}.") from None
    if not chosen:
        raise ValueError("Pick at least one channel.")
    return tuple(channel for channel in CHANNELS if channel in chosen)


def sent_channels(reminder: Reminder) -> list[Channel]:
    """The channels that already delivered this reminder, in a fixed order."""
    stored = {name for name in (reminder.sent_channels or "").split(",") if name}
    return [channel for channel in CHANNELS if channel.value in stored]


@dataclass
class SendOutcome:
    reminder: Reminder
    warnings: list[str] = field(default_factory=list)


def send_reminder(
    session: Session,
    reminder: Reminder,
    gateway: PayPalGateway,
    mailer: Mailer | None = None,
    channels: str | Iterable[str] | None = (Channel.PAYPAL,),
) -> SendOutcome:
    """Send an approved draft. This is the only path out of the app (ADR 0003).

    The draft is claimed before anything is sent, so a double click or a
    second tab gets a ValueError instead of a second reminder (issue #48).
    Each channel is recorded as soon as it succeeds. If a later channel fails,
    the claim is released, the draft is pending again, and a retry skips the
    channels already done, so PayPal never sends the same reminder twice.
    PayPal's attempt is saved before the call, so a retry also skips PayPal
    when the save after the call failed (issue #75). Email works the same way,
    and a mail server whose answer was lost after it took the message counts
    as sent (issue #118).
    """
    if reminder.status != ReminderStatus.PENDING:
        raise ValueError("Only a pending draft can be sent.")
    channels = parse_channels(channels)
    invoice = session.get(Invoice, reminder.invoice_id)
    if invoice is None:
        raise ValueError("This reminder has no invoice.")
    if invoice.status in CLOSED_STATUSES:
        reminder.status = ReminderStatus.REJECTED
        session.add(reminder)
        session.commit()
        raise ValueError("This invoice is already closed, so the draft was rejected.")
    if Channel.PAYPAL in channels and not invoice.paypal_invoice_id:
        raise ValueError("This invoice has no PayPal id yet.")
    if Channel.EMAIL in channels and mailer is None:
        raise ValueError("Email is not available.")

    subject = f"Reminder: invoice {invoice.invoice_number}"
    warnings: list[str] = []

    def via_paypal() -> None:
        if reminder.paypal_attempted_at is not None:
            # An earlier approval asked PayPal, then failed before it could
            # record the result. PayPal may have sent it, so count it as sent,
            # as a timeout does (issue #75).
            warnings.append(
                "An earlier approval asked PayPal to send this reminder but could not record the result. "
                "Duebell counts it as sent so it never goes twice. Check PayPal."
            )
            return
        # Save the attempt before the call. If anything after the call fails,
        # the retry sees it and skips PayPal (issue #75).
        reminder.paypal_attempted_at = utcnow()
        session.add(reminder)
        session.commit()
        try:
            gateway.send_reminder(invoice.paypal_invoice_id, subject, reminder.draft_text)
        except PayPalTimeout:
            # PayPal may still send it. Counting it as sent risks one missed
            # reminder; retrying risks a duplicate to the client (ADR 0003).
            warnings.append(
                "PayPal did not confirm the reminder in time and may still send it. "
                "Duebell counts it as sent so it never goes twice. Check PayPal."
            )
        except PayPalError:
            # PayPal refused the call, so nothing went out. Free PayPal for a
            # retry. If this save fails too, the retry skips PayPal instead.
            reminder.paypal_attempted_at = None
            session.add(reminder)
            session.commit()
            raise

    def via_email() -> None:
        client = session.get(Client, invoice.client_id)
        if client is None:
            raise ValueError("This invoice has no client email.")
        if reminder.email_attempted_at is not None:
            # An earlier approval handed the email to the mail server, then
            # failed before it could record the result (issue #118).
            warnings.append(
                "An earlier approval handed this reminder to the mail server but could not record the result. "
                "Duebell counts the email as sent so it never goes twice. Check your sent mail."
            )
            return
        # Save the attempt before the send, as PayPal does (issues #75, #118).
        reminder.email_attempted_at = utcnow()
        session.add(reminder)
        session.commit()
        try:
            mailer.send(client.email, subject, reminder.draft_text)
        except MailMaybeSent:
            # The server may have the message. Counting it as sent risks one
            # missed email; retrying risks a duplicate to the client.
            warnings.append(
                "The mail server did not confirm the reminder email and may still deliver it. "
                "Duebell counts it as sent so it never goes twice. Check your sent mail."
            )
        except MailError:
            # The server did not take the message. Free email for a retry.
            reminder.email_attempted_at = None
            session.add(reminder)
            session.commit()
            raise

    senders = {Channel.PAYPAL: via_paypal, Channel.EMAIL: via_email}
    if not _claim(session, reminder):
        raise ValueError("This reminder is already being sent.")
    try:
        done = sent_channels(reminder)
        for channel in channels:
            if channel in done:
                continue
            senders[channel]()
            done.append(channel)
            reminder.sent_channels = ",".join(channel.value for channel in CHANNELS if channel in done)
            # When a channel last delivered. The status says whether every chosen
            # channel has, so a partly sent draft still shows in the activity feed.
            reminder.sent_at = utcnow()
            session.add(reminder)
            session.commit()
    except Exception:
        _release(session, reminder)
        raise

    reminder.status = ReminderStatus.SENT
    reminder.sent_at = utcnow()
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return SendOutcome(reminder, warnings)


def _claim(session: Session, reminder: Reminder) -> bool:
    """Move PENDING to SENDING in one conditional update. Only one approval wins."""
    result = session.exec(
        update(Reminder)
        .where(Reminder.id == reminder.id, Reminder.status == ReminderStatus.PENDING)
        .values(status=ReminderStatus.SENDING)
    )
    session.commit()
    session.refresh(reminder)
    return result.rowcount == 1


def _release(session: Session, reminder: Reminder) -> None:
    """Put a claimed draft back to PENDING. Channels already recorded stay recorded."""
    session.rollback()
    session.refresh(reminder)
    reminder.status = ReminderStatus.PENDING
    session.add(reminder)
    session.commit()


def release_interrupted_reminders(session: Session) -> int:
    """Return drafts a stopped process left SENDING to PENDING. Run at startup.

    A retry skips the channels already recorded in sent_channels (issue #48).
    """
    result = session.exec(
        update(Reminder).where(Reminder.status == ReminderStatus.SENDING).values(status=ReminderStatus.PENDING)
    )
    session.commit()
    return result.rowcount


def edit_reminder(session: Session, reminder: Reminder, text: str) -> Reminder:
    if reminder.status != ReminderStatus.PENDING:
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
    if reminder.status != ReminderStatus.PENDING:
        raise ValueError("Only a pending draft can be rejected.")
    reminder.status = ReminderStatus.REJECTED
    session.add(reminder)
    session.commit()
    session.refresh(reminder)
    return reminder


def reminder_rows(session: Session, scope: Scope) -> list[dict]:
    reminders = session.exec(owned(select(Reminder), Reminder, scope).order_by(Reminder.created_at.desc())).all()
    invoices = {invoice.id: invoice for invoice in session.exec(owned(select(Invoice), Invoice, scope)).all()}
    clients = {client.id: client for client in session.exec(owned(select(Client), Client, scope)).all()}
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
                "amount_display": money(invoice.amount_cents, invoice.currency) if invoice else "",
                "currency": invoice.currency if invoice else "",
                "due_display": invoice.due_at.strftime("%d %b %Y") if invoice else "",
                "draft_text": reminder.draft_text,
                "tone": reminder.tone,
                "status": reminder.status,
                "created_display": reminder.created_at.strftime("%d %b %H:%M"),
                "sent_at": reminder.sent_at.isoformat(timespec="minutes") if reminder.sent_at else None,
                "sent_at_display": reminder.sent_at.strftime("%d %b %H:%M") if reminder.sent_at else "",
                "sent_channels": sent_channels(reminder),
                "paypal_sent": Channel.PAYPAL in sent_channels(reminder),
                "has_paypal_id": bool(invoice and invoice.paypal_invoice_id),
            }
        )
    return rows
