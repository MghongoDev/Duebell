"""Recurring invoices and instalment plans (issues #24, #25, #39, #40, #43, #74, #77, #78; ADRs 0003, 0007, 0008).

A schedule is the rule: repeat an invoice every interval, or split one amount
into instalments. Each future invoice is a ScheduledInvoice. The scan moves due
ones to PENDING, and only a user's approval creates the PayPal invoice, through
the same path as the form. The scheduler drafts; a person sends.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, update

from app.config import Settings
from app.currency import from_minor_units
from app.models import (
    Client,
    IntervalUnit,
    Invoice,
    InvoiceStatus,
    Schedule,
    ScheduledInvoice,
    ScheduledInvoiceStatus,
    ScheduleKind,
    ScheduleStatus,
    as_utc,
    utcnow,
)
from app.ownership import Scope, owned
from app.paypal import PayPalError, PayPalGateway
from app.services import (
    SENT_STATUSES,
    InvoiceNotSent,
    draft_invoice,
    money,
    on_held_repeat_sent,
    publish_invoice,
    send_unsent_invoice,
)
from app.validation import FieldError, InvoiceProposal

INTERVAL_UNITS = tuple(IntervalUnit)
MIN_INSTALMENTS = 2
MAX_INSTALMENTS = 6
MAX_CYCLES = 60
MAX_INTERVAL_COUNT = 12
OPEN_STATUSES = (ScheduledInvoiceStatus.UPCOMING, ScheduledInvoiceStatus.PENDING, ScheduledInvoiceStatus.SENDING)


@dataclass(frozen=True)
class ScheduleBasis:
    """What every invoice a schedule produces has in common."""

    user_id: int | None
    client_id: int
    description: str
    amount_cents: int
    currency: str
    terms_days: int


@dataclass(frozen=True)
class ScheduleRequest:
    """The optional repeat or instalment fields of the new invoice form."""

    repeat: str | None = None
    repeat_cycles: int | None = None
    instalments: int = 0
    instalment_interval: str = IntervalUnit.MONTH


def parse_schedule_request(
    repeat: str, repeat_cycles: str, instalments: str, instalment_interval: str
) -> tuple[ScheduleRequest, list[FieldError]]:
    """Read and check the form fields. All the limits live in this module."""
    errors: list[FieldError] = []
    repeat_unit = (repeat or "").strip().upper() or None
    cycles = None
    if repeat_unit:
        if repeat_unit not in INTERVAL_UNITS:
            errors.append(FieldError("repeat", "Repeat weekly or monthly, or leave it off."))
        if (repeat_cycles or "").strip():
            try:
                cycles = int(repeat_cycles)
            except ValueError:
                cycles = 0
            if not 1 <= cycles <= MAX_CYCLES:
                errors.append(
                    FieldError("repeat_cycles", f"Repeats must be a whole number from 1 to {MAX_CYCLES}.")
                )
    count = 0
    interval = (instalment_interval or IntervalUnit.MONTH).strip().upper()
    if (instalments or "").strip() not in ("", "0"):
        try:
            count = int(instalments)
        except ValueError:
            count = -1
        if not MIN_INSTALMENTS <= count <= MAX_INSTALMENTS:
            errors.append(
                FieldError("instalments", f"Choose between {MIN_INSTALMENTS} and {MAX_INSTALMENTS} instalments.")
            )
        if interval not in INTERVAL_UNITS:
            errors.append(FieldError("instalment_interval", "Space instalments weekly or monthly."))
    if repeat_unit and count:
        errors.append(FieldError("repeat", "An invoice can repeat or be split into instalments, not both."))
    request = ScheduleRequest(
        repeat=repeat_unit, repeat_cycles=cycles, instalments=max(count, 0), instalment_interval=interval
    )
    return request, errors


def add_interval(start: datetime, unit: str, count: int) -> datetime:
    if unit == IntervalUnit.WEEK:
        return start + timedelta(weeks=count)
    if unit == IntervalUnit.MONTH:
        month_index = start.month - 1 + count
        year = start.year + month_index // 12
        month = month_index % 12 + 1
        day = min(start.day, calendar.monthrange(year, month)[1])
        return start.replace(year=year, month=month, day=day)
    raise ValueError(f"Unknown interval: {unit}.")


def split_amount(total_cents: int, parts: int) -> list[int]:
    """Split minor units into parts that differ by at most one unit and sum exactly."""
    if not MIN_INSTALMENTS <= parts <= MAX_INSTALMENTS:
        raise ValueError(f"Choose between {MIN_INSTALMENTS} and {MAX_INSTALMENTS} instalments.")
    base, extra = divmod(total_cents, parts)
    if base == 0:
        raise ValueError("The amount is too small to split.")
    return [base + (1 if index < extra else 0) for index in range(parts)]


def _check_interval(unit: str, count: int) -> None:
    if unit not in INTERVAL_UNITS:
        raise ValueError("Repeat weekly or monthly.")
    if not 1 <= count <= MAX_INTERVAL_COUNT:
        raise ValueError(f"The interval must be between 1 and {MAX_INTERVAL_COUNT}.")


def _new_schedule(
    session: Session,
    basis: ScheduleBasis,
    kind: ScheduleKind,
    unit: str,
    count: int,
    cycles_left: int | None,
    starts_on: datetime | None = None,
) -> Schedule:
    _check_interval(unit, count)
    schedule = Schedule(
        user_id=basis.user_id,
        kind=kind,
        client_id=basis.client_id,
        description=basis.description,
        amount_cents=basis.amount_cents,
        currency=basis.currency,
        terms_days=basis.terms_days,
        interval_unit=unit,
        interval_count=count,
        cycles_left=cycles_left,
        starts_on=starts_on,
    )
    session.add(schedule)
    session.commit()
    session.refresh(schedule)
    return schedule


def create_recurring(
    session: Session,
    basis: ScheduleBasis,
    *,
    interval_unit: str,
    start: datetime,
    interval_count: int = 1,
    cycles: int | None = None,
) -> Schedule:
    """Repeat an invoice. `start` is the date of the invoice just sent.

    Occurrence n falls n intervals after `start`, clamped to the month's
    length, so a repeat from 31 January runs on 28 February, then 31 March
    (issue #43).
    """
    if cycles is not None and not 1 <= cycles <= MAX_CYCLES:
        raise ValueError(f"The number of repeats must be between 1 and {MAX_CYCLES}.")
    start = as_utc(start)
    schedule = _new_schedule(
        session, basis, ScheduleKind.RECURRING, interval_unit, interval_count, cycles, starts_on=start
    )
    _queue_next(session, schedule, _issue_on(session, schedule, 1), sequence=1)
    session.commit()
    return schedule


def repeat_invoice(session: Session, invoice: Invoice, unit: str, cycles: int | None) -> Schedule:
    """Repeat a sent invoice: the next one falls one interval after its issue date."""
    basis = ScheduleBasis(
        user_id=invoice.user_id,
        client_id=invoice.client_id,
        description=invoice.description,
        amount_cents=invoice.amount_cents,
        currency=invoice.currency,
        terms_days=invoice.terms_days,
    )
    return create_recurring(
        session, basis, interval_unit=unit, cycles=cycles, start=invoice.issued_at
    )


def hold_repeat(session: Session, invoice: Invoice, unit: str, cycles: int | None) -> None:
    """Keep a Repeat ticked on the form until the invoice is sent (issue #41)."""
    invoice.repeat_unit = unit
    invoice.repeat_cycles = cycles
    session.add(invoice)
    session.commit()


def start_held_repeat(session: Session, invoice: Invoice) -> Schedule | None:
    """Start the repeat held on an invoice PayPal has sent (issues #41, #78).

    Whatever moved the invoice out of DRAFT calls this: the Send button, a
    webhook or a Refresh. The hold is cleared with one conditional update in
    the same commit that creates the schedule, so when two of them race only
    one starts a schedule. Returns None if there was nothing to start.
    """
    unit, cycles = invoice.repeat_unit, invoice.repeat_cycles
    if not unit or invoice.status not in SENT_STATUSES:
        return None
    claimed = session.exec(
        update(Invoice)
        .where(Invoice.id == invoice.id, Invoice.repeat_unit.is_not(None))
        .values(repeat_unit=None, repeat_cycles=None)
    )
    if claimed.rowcount != 1:
        session.rollback()
        session.refresh(invoice)
        return None
    schedule = repeat_invoice(session, invoice, unit, cycles)
    session.refresh(invoice)
    return schedule


on_held_repeat_sent(start_held_repeat)


def send_draft(
    session: Session, invoice: Invoice, gateway: PayPalGateway, *, warnings: list[str] | None = None
) -> Invoice:
    """Send a draft PayPal holds but has not sent, then start its held repeat (issue #41).

    The send reuses the PayPal invoice, so it never creates a second one.
    Raises ValueError if the invoice is not such a draft, InvoiceNotSent if
    PayPal refuses.
    """
    invoice = send_unsent_invoice(session, invoice, gateway, warnings=warnings)
    start_held_repeat(session, invoice)
    return invoice


def create_plan(
    session: Session,
    basis: ScheduleBasis,
    *,
    instalments: int,
    interval_unit: str = IntervalUnit.MONTH,
    interval_count: int = 1,
    start: datetime | None = None,
) -> Schedule:
    """Split one amount into instalments. Each waits for approval when due."""
    amounts = split_amount(basis.amount_cents, instalments)
    start = as_utc(start or utcnow())
    schedule = _new_schedule(session, basis, ScheduleKind.INSTALMENT, interval_unit, interval_count, 0)
    for index, cents in enumerate(amounts):
        session.add(
            ScheduledInvoice(
                user_id=schedule.user_id,
                schedule_id=schedule.id,
                sequence=index + 1,
                amount_cents=cents,
                issue_on=add_interval(start, interval_unit, interval_count * index),
                description=f"{basis.description} (instalment {index + 1} of {instalments})",
            )
        )
    session.commit()
    return schedule


def _queue_next(session: Session, schedule: Schedule, issue_on: datetime, sequence: int) -> None:
    if schedule.status != ScheduleStatus.ACTIVE:
        return
    if schedule.cycles_left is not None:
        if schedule.cycles_left <= 0:
            # Nothing more to queue. The schedule finishes once its last
            # invoice is approved or skipped, so it stays cancellable until then.
            return
        schedule.cycles_left -= 1
        session.add(schedule)
    session.add(
        ScheduledInvoice(
            user_id=schedule.user_id,
            schedule_id=schedule.id,
            sequence=sequence,
            amount_cents=schedule.amount_cents,
            issue_on=issue_on,
            description=schedule.description,
        )
    )


def _issue_on(session: Session, schedule: Schedule, sequence: int) -> datetime:
    """The date of a repeat's occurrence, counted from its start (issue #43).

    Counting from the start, not from the previous date, keeps the day of the
    month: a date clamped to 28 February does not pull later months back to
    the 28th. A repeat an older build made has no start, so it counts from its
    earliest scheduled invoice instead.
    """
    unit, count = schedule.interval_unit, schedule.interval_count
    if schedule.starts_on is not None:
        return add_interval(as_utc(schedule.starts_on), unit, count * sequence)
    first = session.exec(
        select(ScheduledInvoice)
        .where(ScheduledInvoice.schedule_id == schedule.id)
        .order_by(ScheduledInvoice.sequence)
    ).first()
    return add_interval(as_utc(first.issue_on), unit, count * (sequence - first.sequence))


def _open_items(session: Session, schedule: Schedule) -> list[ScheduledInvoice]:
    query = select(ScheduledInvoice).where(
        ScheduledInvoice.schedule_id == schedule.id,
        ScheduledInvoice.status.in_(OPEN_STATUSES),
    )
    return list(session.exec(query).all())


def draft_scheduled(session: Session, now: datetime | None = None, *, scope: Scope) -> list[ScheduledInvoice]:
    """Move due scheduled invoices to PENDING. Nothing reaches PayPal here.

    The scheduler and a manual scan can run at once. Each item is claimed with
    one conditional update, and only the scan that wins queues the next
    occurrence, so a repeat never gains two (issue #40).
    """
    now = as_utc(now or utcnow())
    query = owned(select(ScheduledInvoice).where(ScheduledInvoice.status == ScheduledInvoiceStatus.UPCOMING), ScheduledInvoice, scope)
    drafted = []
    for item in session.exec(query).all():
        if as_utc(item.issue_on) > now:
            continue
        schedule = session.get(Schedule, item.schedule_id)
        if schedule is None or schedule.status == ScheduleStatus.CANCELLED:
            continue
        if not _claim_upcoming(session, item):
            session.rollback()
            continue
        if schedule.kind == ScheduleKind.RECURRING:
            # Read the schedule again inside the claim, so cycles_left is current.
            session.refresh(schedule)
            next_sequence = item.sequence + 1
            _queue_next(session, schedule, _issue_on(session, schedule, next_sequence), next_sequence)
        try:
            session.commit()
        except IntegrityError:
            # The next occurrence already exists. The unique index on
            # (schedule_id, sequence) refused a second, and the rollback also
            # undid the claim. Claim the item again and skip only the queueing,
            # so it still reaches PENDING (issue #40).
            session.rollback()
            if not _claim_upcoming(session, item):
                session.rollback()
                continue
            session.commit()
        session.refresh(item)
        drafted.append(item)
    return drafted


def _claim_upcoming(session: Session, item: ScheduledInvoice) -> bool:
    """Move UPCOMING to PENDING in one conditional update. Only one scan wins."""
    result = session.exec(
        update(ScheduledInvoice)
        .where(ScheduledInvoice.id == item.id, ScheduledInvoice.status == ScheduledInvoiceStatus.UPCOMING)
        .values(status=ScheduledInvoiceStatus.PENDING)
    )
    return result.rowcount == 1


def _claim(session: Session, item: ScheduledInvoice) -> bool:
    """Move PENDING to SENDING in one conditional update. Only one approval wins."""
    result = session.exec(
        update(ScheduledInvoice)
        .where(ScheduledInvoice.id == item.id, ScheduledInvoice.status == ScheduledInvoiceStatus.PENDING)
        .values(status=ScheduledInvoiceStatus.SENDING)
    )
    session.commit()
    session.refresh(item)
    return result.rowcount == 1


def _release(session: Session, item: ScheduledInvoice) -> None:
    """Put a claimed item back to PENDING after a failed approval (issue #77).

    One conditional update moves it only if it is still SENDING. If its
    schedule was cancelled while PayPal was called, it stays CANCELLED. The
    link to its invoice is kept, so a draft PayPal holds unsent stays with the
    user, who can send it from its row or leave it.
    """
    session.rollback()
    _unclaim(session, item)


def _unclaim(session: Session, item: ScheduledInvoice) -> None:
    """Move SENDING back to PENDING in one conditional update, then commit (issue #77)."""
    session.exec(
        update(ScheduledInvoice)
        .where(ScheduledInvoice.id == item.id, ScheduledInvoice.status == ScheduledInvoiceStatus.SENDING)
        .values(status=ScheduledInvoiceStatus.PENDING)
    )
    session.commit()
    session.refresh(item)


def _drop_unpublished(session: Session, item: ScheduledInvoice) -> None:
    """Release the item after PayPal refused to create its invoice (issue #74).

    As on the form, the local draft PayPal never took is deleted. The item is
    unlinked in the same commit, so the next approval drafts afresh. The item
    goes back to PENDING only if it is still SENDING; one whose schedule was
    cancelled meanwhile stays CANCELLED, its draft deleted all the same
    (issue #77).
    """
    session.rollback()
    session.refresh(item)
    linked = session.get(Invoice, item.invoice_id) if item.invoice_id is not None else None
    if linked is not None and linked.status == InvoiceStatus.DRAFT and not linked.paypal_invoice_id:
        item.invoice_id = None
        session.add(item)
        session.delete(linked)
        session.flush()
    _unclaim(session, item)


def release_interrupted(session: Session) -> int:
    """Return items a stopped process left SENDING to PENDING. Run at startup.

    A retry is safe: approval links the item to its invoice before PayPal is
    called, so the next approval finishes that invoice instead of creating
    another (issue #74).
    """
    result = session.exec(
        update(ScheduledInvoice)
        .where(ScheduledInvoice.status == ScheduledInvoiceStatus.SENDING)
        .values(status=ScheduledInvoiceStatus.PENDING)
    )
    session.commit()
    return result.rowcount


def approve_scheduled(
    session: Session,
    item: ScheduledInvoice,
    settings: Settings,
    gateway: PayPalGateway,
    *,
    warnings: list[str] | None = None,
) -> Invoice:
    """Create and send the invoice through the normal path. The only way out.

    The item is claimed before PayPal is called, so a double click or a second
    tab gets a 409 instead of a second invoice (issue #39). The local draft is
    saved and linked to the item, committed, before PayPal is called. A retry
    then picks up where the last one stopped: it creates a linked draft in
    PayPal if PayPal does not hold it yet, sends it if PayPal holds it unsent,
    and marks the item sent if PayPal has sent it (issue #74).

    An item whose schedule is not ACTIVE is refused. If the schedule is
    cancelled while PayPal is called and the call fails, the item stays
    CANCELLED (issue #77).
    """
    if item.status != ScheduledInvoiceStatus.PENDING:
        raise ValueError("Only a pending scheduled invoice can be approved.")
    schedule = session.get(Schedule, item.schedule_id)
    client = session.get(Client, schedule.client_id) if schedule else None
    if schedule is None or client is None:
        raise ValueError("This scheduled invoice has lost its client.")
    if schedule.status != ScheduleStatus.ACTIVE:
        raise ValueError("This schedule is no longer active.")
    if not _claim(session, item):
        raise ValueError("This scheduled invoice is already being approved.")
    try:
        invoice = session.get(Invoice, item.invoice_id) if item.invoice_id is not None else None
        if invoice is None:
            proposal = InvoiceProposal(
                client_name=client.name,
                client_email=client.email,
                description=item.description,
                amount=from_minor_units(item.amount_cents, schedule.currency),
                currency=schedule.currency,
                terms_days=schedule.terms_days,
            )
            # Link the saved draft before calling PayPal, so a retry after
            # a failed commit or a restart finds this invoice (issue #74).
            invoice = draft_invoice(session, proposal, user_id=schedule.user_id)
            item.invoice_id = invoice.id
            session.add(item)
            session.commit()
        if invoice.status != InvoiceStatus.DRAFT:
            # PayPal sent it after all, for example after a timeout; a webhook
            # or refresh has said so. Nothing more to send.
            pass
        elif invoice.paypal_invoice_id:
            # An earlier approval created the PayPal invoice but could not send it.
            invoice = send_unsent_invoice(session, invoice, gateway, warnings=warnings)
        else:
            # A new draft, or one a restart left before PayPal created it.
            invoice = publish_invoice(session, invoice, gateway, warnings=warnings)
    except InvoiceNotSent:
        _release(session, item)
        raise
    except PayPalError:
        _drop_unpublished(session, item)
        raise
    except Exception:
        _release(session, item)
        raise
    item.status = ScheduledInvoiceStatus.SENT
    item.invoice_id = invoice.id
    session.add(item)
    session.commit()
    _finish_if_done(session, schedule)
    return invoice


def reject_scheduled(session: Session, item: ScheduledInvoice) -> ScheduledInvoice:
    if item.status != ScheduledInvoiceStatus.PENDING:
        raise ValueError("Only a pending scheduled invoice can be rejected.")
    item.status = ScheduledInvoiceStatus.REJECTED
    session.add(item)
    session.commit()
    schedule = session.get(Schedule, item.schedule_id)
    if schedule:
        _finish_if_done(session, schedule)
    return item


def cancel_schedule(session: Session, schedule: Schedule) -> Schedule:
    """Stop future drafts. Invoices already sent are left alone."""
    schedule.status = ScheduleStatus.CANCELLED
    session.add(schedule)
    for item in _open_items(session, schedule):
        item.status = ScheduledInvoiceStatus.CANCELLED
        session.add(item)
    session.commit()
    session.refresh(schedule)
    return schedule


def _finish_if_done(session: Session, schedule: Schedule) -> None:
    """Mark a schedule DONE when it can produce nothing more and nothing is open."""
    if schedule.status != ScheduleStatus.ACTIVE or schedule.cycles_left != 0:
        return
    if not _open_items(session, schedule):
        schedule.status = ScheduleStatus.DONE
        session.add(schedule)
        session.commit()


def schedule_rows(session: Session, scope: Scope) -> dict:
    clients = {client.id: client for client in session.exec(owned(select(Client), Client, scope)).all()}
    schedules = session.exec(owned(select(Schedule), Schedule, scope).order_by(Schedule.id.desc())).all()
    items = session.exec(
        owned(select(ScheduledInvoice), ScheduledInvoice, scope).order_by(ScheduledInvoice.issue_on)
    ).all()
    by_schedule: dict[int, list[ScheduledInvoice]] = {}
    for item in items:
        by_schedule.setdefault(item.schedule_id, []).append(item)

    pending, active = [], []
    for schedule in schedules:
        client = clients.get(schedule.client_id)
        name = client.name if client else ""
        own = by_schedule.get(schedule.id, [])
        for item in own:
            # Only an active schedule's items can be approved (issue #77).
            if item.status == ScheduledInvoiceStatus.PENDING and schedule.status == ScheduleStatus.ACTIVE:
                pending.append(
                    {
                        "id": item.id,
                        "client_name": name,
                        "description": item.description,
                        "amount_display": money(item.amount_cents, schedule.currency),
                        "currency": schedule.currency,
                        "issue_display": as_utc(item.issue_on).strftime("%d %b %Y"),
                        "kind": schedule.kind,
                    }
                )
        if schedule.status == ScheduleStatus.ACTIVE:
            upcoming = [item for item in own if item.status == ScheduledInvoiceStatus.UPCOMING]
            every = IntervalUnit(schedule.interval_unit).noun
            if schedule.interval_count > 1:
                every = f"{schedule.interval_count} {every}s"
            active.append(
                {
                    "id": schedule.id,
                    "kind": schedule.kind,
                    "client_name": name,
                    "description": schedule.description,
                    "amount_display": money(schedule.amount_cents, schedule.currency),
                    "currency": schedule.currency,
                    "every": every,
                    "next_display": as_utc(upcoming[0].issue_on).strftime("%d %b %Y") if upcoming else "",
                    "sent": sum(1 for item in own if item.status == ScheduledInvoiceStatus.SENT),
                    "total": len(own) if schedule.kind == ScheduleKind.INSTALMENT else None,
                }
            )
    return {"pending": pending, "active": active}
