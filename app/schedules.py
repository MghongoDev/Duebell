"""Recurring invoices and instalment plans (issue #25).

A schedule is the rule: repeat an invoice every interval, or split one amount
into instalments. Each future invoice is a ScheduledInvoice. The scan moves due
ones to PENDING, and only an operator's approval creates the PayPal invoice,
through the same path as the form. The scheduler drafts; a person sends
(ADR 0003).
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlmodel import Session, select

from app.config import Settings
from app.models import Client, Schedule, ScheduledInvoice, utcnow
from app.services import create_invoice
from app.validation import InvoiceProposal

INTERVAL_UNITS = ("WEEK", "MONTH")
MIN_INSTALMENTS = 2
MAX_INSTALMENTS = 6
MAX_CYCLES = 60


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


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
    if not 1 <= count <= 12:
        raise ValueError("The interval must be between 1 and 12.")


def create_recurring(
    session: Session,
    *,
    client_id: int,
    description: str,
    amount_cents: int,
    currency: str,
    terms_days: int,
    interval_unit: str,
    interval_count: int = 1,
    cycles: int | None = None,
    first_issue: datetime,
    user_id: int | None = None,
) -> Schedule:
    """Repeat an invoice. `first_issue` is the next one after the invoice just sent."""
    _check_interval(interval_unit, interval_count)
    if cycles is not None and not 1 <= cycles <= MAX_CYCLES:
        raise ValueError(f"The number of repeats must be between 1 and {MAX_CYCLES}.")
    schedule = Schedule(
        user_id=user_id,
        kind="RECURRING",
        client_id=client_id,
        description=description,
        amount_cents=amount_cents,
        currency=currency,
        terms_days=terms_days,
        interval_unit=interval_unit,
        interval_count=interval_count,
        cycles_left=cycles,
    )
    session.add(schedule)
    session.commit()
    session.refresh(schedule)
    _queue_next(session, schedule, first_issue, sequence=1)
    session.commit()
    return schedule


def create_plan(
    session: Session,
    *,
    client_id: int,
    description: str,
    amount_cents: int,
    currency: str,
    terms_days: int,
    instalments: int,
    interval_unit: str = "MONTH",
    interval_count: int = 1,
    start: datetime | None = None,
    user_id: int | None = None,
) -> Schedule:
    _check_interval(interval_unit, interval_count)
    amounts = split_amount(amount_cents, instalments)
    start = _aware(start or utcnow())
    schedule = Schedule(
        user_id=user_id,
        kind="INSTALMENT",
        client_id=client_id,
        description=description,
        amount_cents=amount_cents,
        currency=currency,
        terms_days=terms_days,
        interval_unit=interval_unit,
        interval_count=interval_count,
        cycles_left=0,
    )
    session.add(schedule)
    session.commit()
    session.refresh(schedule)
    for index, cents in enumerate(amounts):
        session.add(
            ScheduledInvoice(
                user_id=schedule.user_id,
                schedule_id=schedule.id,
                sequence=index + 1,
                amount_cents=cents,
                issue_on=add_interval(start, interval_unit, interval_count * index),
                description=f"{description} (instalment {index + 1} of {instalments})",
            )
        )
    session.commit()
    return schedule


def _queue_next(session: Session, schedule: Schedule, issue_on: datetime, sequence: int) -> None:
    if schedule.status != "ACTIVE":
        return
    if schedule.cycles_left is not None:
        if schedule.cycles_left <= 0:
            schedule.status = "DONE"
            session.add(schedule)
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


def draft_scheduled(
    session: Session, now: datetime | None = None, user_id: int | None = None
) -> list[ScheduledInvoice]:
    """Move due scheduled invoices to PENDING. Nothing reaches PayPal here."""
    now = _aware(now or utcnow())
    query = select(ScheduledInvoice).where(ScheduledInvoice.status == "UPCOMING")
    if user_id is not None:
        query = query.where(ScheduledInvoice.user_id == user_id)
    due = session.exec(query).all()
    drafted = []
    for item in due:
        if _aware(item.issue_on) > now:
            continue
        schedule = session.get(Schedule, item.schedule_id)
        if schedule is None or schedule.status == "CANCELLED":
            continue
        item.status = "PENDING"
        session.add(item)
        drafted.append(item)
        if schedule.kind == "RECURRING":
            next_issue = add_interval(_aware(item.issue_on), schedule.interval_unit, schedule.interval_count)
            _queue_next(session, schedule, next_issue, item.sequence + 1)
    if drafted:
        session.commit()
    return drafted


def approve_scheduled(session: Session, item: ScheduledInvoice, settings: Settings, gateway):
    """Create and send the invoice through the normal path. The only way out."""
    if item.status != "PENDING":
        raise ValueError("Only a pending scheduled invoice can be approved.")
    schedule = session.get(Schedule, item.schedule_id)
    client = session.get(Client, schedule.client_id) if schedule else None
    if schedule is None or client is None:
        raise ValueError("This scheduled invoice has lost its client.")
    proposal = InvoiceProposal(
        client_name=client.name,
        client_email=client.email,
        description=item.description,
        amount=Decimal(item.amount_cents) / 100,
        currency=schedule.currency,
        terms_days=schedule.terms_days,
    )
    invoice = create_invoice(session, proposal, settings, gateway, user_id=schedule.user_id)
    item.status = "SENT"
    item.invoice_id = invoice.id
    session.add(item)
    session.commit()
    _finish_plan_if_done(session, schedule)
    return invoice


def reject_scheduled(session: Session, item: ScheduledInvoice) -> ScheduledInvoice:
    if item.status != "PENDING":
        raise ValueError("Only a pending scheduled invoice can be rejected.")
    item.status = "REJECTED"
    session.add(item)
    session.commit()
    schedule = session.get(Schedule, item.schedule_id)
    if schedule:
        _finish_plan_if_done(session, schedule)
    return item


def cancel_schedule(session: Session, schedule: Schedule) -> Schedule:
    """Stop future drafts. Invoices already sent are left alone."""
    schedule.status = "CANCELLED"
    session.add(schedule)
    items = session.exec(
        select(ScheduledInvoice).where(
            ScheduledInvoice.schedule_id == schedule.id,
            ScheduledInvoice.status.in_(("UPCOMING", "PENDING")),
        )
    ).all()
    for item in items:
        item.status = "CANCELLED"
        session.add(item)
    session.commit()
    session.refresh(schedule)
    return schedule


def _finish_plan_if_done(session: Session, schedule: Schedule) -> None:
    if schedule.kind != "INSTALMENT" or schedule.status != "ACTIVE":
        return
    open_items = session.exec(
        select(ScheduledInvoice).where(
            ScheduledInvoice.schedule_id == schedule.id,
            ScheduledInvoice.status.in_(("UPCOMING", "PENDING")),
        )
    ).all()
    if not open_items:
        schedule.status = "DONE"
        session.add(schedule)
        session.commit()


def schedule_rows(session: Session, user_id: int | None = None) -> dict:
    def mine(query, model):
        return query if user_id is None else query.where(model.user_id == user_id)

    clients = {client.id: client for client in session.exec(mine(select(Client), Client)).all()}
    schedules = session.exec(mine(select(Schedule), Schedule).order_by(Schedule.id.desc())).all()
    items = session.exec(mine(select(ScheduledInvoice), ScheduledInvoice).order_by(ScheduledInvoice.issue_on)).all()
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
                        "amount_display": f"{item.amount_cents / 100:,.2f}",
                        "currency": schedule.currency,
                        "issue_display": _aware(item.issue_on).strftime("%d %b %Y"),
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
                    "amount_display": f"{schedule.amount_cents / 100:,.2f}",
                    "currency": schedule.currency,
                    "every": every,
                    "next_display": _aware(upcoming[0].issue_on).strftime("%d %b %Y") if upcoming else "",
                    "sent": sum(1 for item in own if item.status == "SENT"),
                    "total": len(own) if schedule.kind == "INSTALMENT" else None,
                }
            )
    return {"pending": pending, "active": active}
