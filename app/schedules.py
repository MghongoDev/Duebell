"""Recurring invoices and instalment plans (issues #24, #25; ADRs 0003, 0007, 0008).

A schedule is the rule: repeat an invoice every interval, or split one amount
into instalments. Each future invoice is a ScheduledInvoice. The scan moves due
ones to PENDING, and only a user's approval creates the PayPal invoice, through
the same path as the form. The scheduler drafts; a person sends.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlmodel import Session, select

from app.config import Settings
from app.models import Client, Invoice, Schedule, ScheduledInvoice, as_utc, utcnow
from app.ownership import Scope, owned
from app.paypal import PayPalGateway
from app.services import InvoiceNotSent, create_invoice, money, send_draft_invoice
from app.validation import FieldError, InvoiceProposal

INTERVAL_UNITS = ("WEEK", "MONTH")
MIN_INSTALMENTS = 2
MAX_INSTALMENTS = 6
MAX_CYCLES = 60
MAX_INTERVAL_COUNT = 12
OPEN_STATUSES = ("UPCOMING", "PENDING")


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
    instalment_interval: str = "MONTH"


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
    interval = (instalment_interval or "MONTH").strip().upper()
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
    if unit == "WEEK":
        return start + timedelta(weeks=count)
    if unit == "MONTH":
        month_index = start.month - 1 + count
        year = start.year + month_index // 12
        month = month_index % 12 + 1
        day = min(start.day, calendar.monthrange(year, month)[1])
        return start.replace(year=year, month=month, day=day)
    raise ValueError(f"Unknown interval: {unit}.")


def split_amount(total_cents: int, parts: int) -> list[int]:
    """Split cents into parts that differ by at most one cent and sum exactly."""
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
    session: Session, basis: ScheduleBasis, kind: str, unit: str, count: int, cycles_left: int | None
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
    first_issue: datetime,
    interval_count: int = 1,
    cycles: int | None = None,
) -> Schedule:
    """Repeat an invoice. `first_issue` is the next one after the invoice just sent."""
    if cycles is not None and not 1 <= cycles <= MAX_CYCLES:
        raise ValueError(f"The number of repeats must be between 1 and {MAX_CYCLES}.")
    schedule = _new_schedule(session, basis, "RECURRING", interval_unit, interval_count, cycles)
    _queue_next(session, schedule, first_issue, sequence=1)
    session.commit()
    return schedule


def create_plan(
    session: Session,
    basis: ScheduleBasis,
    *,
    instalments: int,
    interval_unit: str = "MONTH",
    interval_count: int = 1,
    start: datetime | None = None,
) -> Schedule:
    """Split one amount into instalments. Each waits for approval when due."""
    amounts = split_amount(basis.amount_cents, instalments)
    start = as_utc(start or utcnow())
    schedule = _new_schedule(session, basis, "INSTALMENT", interval_unit, interval_count, 0)
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
    if schedule.status != "ACTIVE":
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


def _open_items(session: Session, schedule: Schedule) -> list[ScheduledInvoice]:
    query = select(ScheduledInvoice).where(
        ScheduledInvoice.schedule_id == schedule.id,
        ScheduledInvoice.status.in_(OPEN_STATUSES),
    )
    return list(session.exec(query).all())


def draft_scheduled(session: Session, now: datetime | None = None, *, scope: Scope) -> list[ScheduledInvoice]:
    """Move due scheduled invoices to PENDING. Nothing reaches PayPal here."""
    now = as_utc(now or utcnow())
    query = owned(select(ScheduledInvoice).where(ScheduledInvoice.status == "UPCOMING"), ScheduledInvoice, scope)
    drafted = []
    for item in session.exec(query).all():
        if as_utc(item.issue_on) > now:
            continue
        schedule = session.get(Schedule, item.schedule_id)
        if schedule is None or schedule.status == "CANCELLED":
            continue
        item.status = "PENDING"
        session.add(item)
        drafted.append(item)
        if schedule.kind == "RECURRING":
            next_issue = add_interval(as_utc(item.issue_on), schedule.interval_unit, schedule.interval_count)
            _queue_next(session, schedule, next_issue, item.sequence + 1)
    if drafted:
        session.commit()
    return drafted


def approve_scheduled(
    session: Session, item: ScheduledInvoice, settings: Settings, gateway: PayPalGateway
) -> Invoice:
    """Create and send the invoice through the normal path. The only way out."""
    if item.status != "PENDING":
        raise ValueError("Only a pending scheduled invoice can be approved.")
    schedule = session.get(Schedule, item.schedule_id)
    client = session.get(Client, schedule.client_id) if schedule else None
    if schedule is None or client is None:
        raise ValueError("This scheduled invoice has lost its client.")
    try:
        if item.invoice_id is not None:
            # An earlier approval created the PayPal draft but could not send it.
            invoice = send_draft_invoice(session, session.get(Invoice, item.invoice_id), gateway)
        else:
            proposal = InvoiceProposal(
                client_name=client.name,
                client_email=client.email,
                description=item.description,
                amount=Decimal(item.amount_cents) / 100,
                currency=schedule.currency,
                terms_days=schedule.terms_days,
            )
            invoice = create_invoice(session, proposal, settings, gateway, user_id=schedule.user_id)
    except InvoiceNotSent as exc:
        item.invoice_id = exc.invoice.id
        session.add(item)
        session.commit()
        raise
    item.status = "SENT"
    item.invoice_id = invoice.id
    session.add(item)
    session.commit()
    _finish_if_done(session, schedule)
    return invoice


def reject_scheduled(session: Session, item: ScheduledInvoice) -> ScheduledInvoice:
    if item.status != "PENDING":
        raise ValueError("Only a pending scheduled invoice can be rejected.")
    item.status = "REJECTED"
    session.add(item)
    session.commit()
    schedule = session.get(Schedule, item.schedule_id)
    if schedule:
        _finish_if_done(session, schedule)
    return item


def cancel_schedule(session: Session, schedule: Schedule) -> Schedule:
    """Stop future drafts. Invoices already sent are left alone."""
    schedule.status = "CANCELLED"
    session.add(schedule)
    for item in _open_items(session, schedule):
        item.status = "CANCELLED"
        session.add(item)
    session.commit()
    session.refresh(schedule)
    return schedule


def _finish_if_done(session: Session, schedule: Schedule) -> None:
    """Mark a schedule DONE when it can produce nothing more and nothing is open."""
    if schedule.status != "ACTIVE" or schedule.cycles_left != 0:
        return
    if not _open_items(session, schedule):
        schedule.status = "DONE"
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
            if item.status == "PENDING":
                pending.append(
                    {
                        "id": item.id,
                        "client_name": name,
                        "description": item.description,
                        "amount_display": money(item.amount_cents),
                        "currency": schedule.currency,
                        "issue_display": as_utc(item.issue_on).strftime("%d %b %Y"),
                        "kind": schedule.kind,
                    }
                )
        if schedule.status == "ACTIVE":
            upcoming = [item for item in own if item.status == "UPCOMING"]
            every = "week" if schedule.interval_unit == "WEEK" else "month"
            if schedule.interval_count > 1:
                every = f"{schedule.interval_count} {every}s"
            active.append(
                {
                    "id": schedule.id,
                    "kind": schedule.kind,
                    "client_name": name,
                    "description": schedule.description,
                    "amount_display": money(schedule.amount_cents),
                    "currency": schedule.currency,
                    "every": every,
                    "next_display": as_utc(upcoming[0].issue_on).strftime("%d %b %Y") if upcoming else "",
                    "sent": sum(1 for item in own if item.status == "SENT"),
                    "total": len(own) if schedule.kind == "INSTALMENT" else None,
                }
            )
    return {"pending": pending, "active": active}
