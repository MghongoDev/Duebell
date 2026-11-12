"""Database models and engine setup for Duebell (issues #9, #22, #24, #25, #26, #31, #40, #41, #42).

Every table that holds a user's data has a user_id (ADR 0008). init_db adds
columns and indexes a newer model declares to an older database, so a demo
database keeps working across builds.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timezone
from enum import StrEnum
from typing import Optional

from sqlalchemy import DateTime, Index, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.types import TypeDecorator
from sqlalchemy.pool import StaticPool
from sqlmodel import Field, SQLModel, create_engine

log = logging.getLogger("duebell.models")

def new_session_key() -> str:
    """A random key that ties a session cookie to one user (issue #142)."""
    return secrets.token_urlsafe(32)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Treat a naive datetime from storage as UTC, so it compares with utcnow()."""
    if value is None or value.tzinfo:
        return value
    return value.replace(tzinfo=timezone.utc)


class UTCDateTime(TypeDecorator):
    """Store UTC and always read back an aware UTC datetime (issue #34).

    SQLite keeps no offset. Declaring the type here, not relying on the
    sqlmodel default, keeps every comparison with utcnow() working whatever
    sqlmodel version is installed. A naive value is taken to be UTC.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else as_utc(value).astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        return None if value is None else as_utc(value).astimezone(timezone.utc)


class InvoiceStatus(StrEnum):
    """Where an invoice stands. PayPal's own status names, stored as is (issue #31)."""

    DRAFT = "DRAFT"
    SENT = "SENT"
    PAID = "PAID"
    MARKED_AS_PAID = "MARKED_AS_PAID"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"
    OVERDUE = "OVERDUE"
    # Still open: the client may owe some or all of the amount (issue #112).
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    # Closed: PayPal's other names for paid, refunded and cancelled (issue #112).
    PAID_EXTERNAL = "PAID_EXTERNAL"
    MARKED_AS_REFUNDED = "MARKED_AS_REFUNDED"
    REFUNDED_EXTERNAL = "REFUNDED_EXTERNAL"
    AUTO_CANCELLED = "AUTO_CANCELLED"


class ReminderStatus(StrEnum):
    PENDING = "PENDING"
    # Claimed by one approval while it sends (issue #48).
    SENDING = "SENDING"
    SENT = "SENT"
    REJECTED = "REJECTED"


class ReminderStage(StrEnum):
    """Where an invoice stood when its reminder was drafted (issue #42)."""

    BEFORE_DUE = "BEFORE_DUE"
    OVERDUE = "OVERDUE"


class ScheduleKind(StrEnum):
    RECURRING = "RECURRING"
    INSTALMENT = "INSTALMENT"


class IntervalUnit(StrEnum):
    """How far apart a schedule's invoices fall."""

    WEEK = "WEEK"
    MONTH = "MONTH"

    @property
    def noun(self) -> str:
        return self.value.lower()


class ScheduleStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class ScheduledInvoiceStatus(StrEnum):
    UPCOMING = "UPCOMING"
    PENDING = "PENDING"
    # Claimed by one approval while it talks to PayPal (issue #39).
    SENDING = "SENDING"
    SENT = "SENT"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class ComparisonStatus(StrEnum):
    OK = "OK"
    NOT_ENOUGH_HISTORY = "NOT_ENOUGH_HISTORY"


class User(SQLModel, table=True):
    """A person who signs in. Every record below belongs to one user (issue #24)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    # Every session cookie carries this key, and sign-in accepts a cookie only
    # while it matches, so a cookie never signs in a later user who reuses this
    # id. A new key signs the user out everywhere (issue #142).
    session_key: Optional[str] = Field(default_factory=new_session_key)


class Client(SQLModel, table=True):
    # Two users may bill the same client email, so email is unique per user.
    __table_args__ = (Index("ux_client_user_email", "user_id", "email", unique=True),)

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    name: str
    email: str = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class Invoice(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    client_id: int = Field(foreign_key="client.id", index=True)
    invoice_number: str = Field(index=True, unique=True)
    paypal_invoice_id: Optional[str] = Field(default=None, unique=True)
    description: str = ""
    amount_cents: int
    currency: str = "USD"
    terms_days: int = 14
    issued_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    due_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    paid_at: Optional[datetime] = Field(default=None, sa_type=UTCDateTime)
    status: str = InvoiceStatus.DRAFT
    risk_score: Optional[float] = None
    risk_level: Optional[str] = None
    risk_reasons: str = ""
    limited_data: bool = False
    # Why the score has limited data, set when the invoice is scored (issue #61).
    # An invoice from an older build has none and shows "Limited data" alone.
    limited_data_reason: Optional[str] = None
    # A Repeat ticked on the form when the send failed. The first send through
    # POST /api/invoices/{id}/send turns it into a schedule and clears it
    # (issue #41). repeat_cycles None with a repeat_unit repeats until cancelled.
    repeat_unit: Optional[str] = None
    repeat_cycles: Optional[int] = None
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class InvoiceNumberCounter(SQLModel, table=True):
    """The last suffix handed out in an invoice number. One row, id 1 (issue #114).

    It only goes up, so a number freed by a deleted draft is never handed out
    again. PayPal may already hold that number.
    """

    id: int = Field(default=1, primary_key=True)
    last: int = 0


class Reminder(SQLModel, table=True):
    # One pending draft per invoice, even if two scans run at once (issue #40).
    __table_args__ = (
        Index("ux_reminder_pending_invoice", "invoice_id", unique=True, sqlite_where=text("status = 'PENDING'")),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    invoice_id: int = Field(foreign_key="invoice.id", index=True)
    draft_text: str
    tone: str = "friendly"
    status: str = ReminderStatus.PENDING
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    sent_at: Optional[datetime] = Field(default=None, sa_type=UTCDateTime)
    # Comma-separated channels that have already delivered this reminder,
    # e.g. "paypal" or "paypal,email". A retry skips them.
    sent_channels: str = ""
    # When Duebell last asked PayPal to send this reminder, saved before the
    # call (issue #75). If PayPal is not in sent_channels, PayPal may have sent
    # it, so a retry skips PayPal. It never counts the reminder as delivered.
    paypal_attempted_at: Optional[datetime] = Field(default=None, sa_type=UTCDateTime)
    # When Duebell last handed this reminder to the mail server, saved before
    # the send (issue #118). If email is not in sent_channels, the server may
    # have taken it, so a retry skips email. It never counts as delivered.
    email_attempted_at: Optional[datetime] = Field(default=None, sa_type=UTCDateTime)
    # BEFORE_DUE or OVERDUE, set when drafted (issue #42). A row from an older
    # build has none; the scan infers it from created_at and the due date.
    stage: Optional[str] = None
    # The tone the user picked with the tone switch, "friendly" or "firm"
    # (issue #152). None if they never picked one, so the scan's choice
    # stands. A later hand edit sets tone to "custom" and keeps this.
    chosen_tone: Optional[str] = None


class Event(SQLModel, table=True):
    paypal_event_id: str = Field(primary_key=True)
    # The owner of the invoice the event touched. None if it matched no invoice.
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    event_type: str
    payload: str = ""
    received_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class Schedule(SQLModel, table=True):
    """A rule that produces future invoices (issue #25).

    RECURRING repeats one amount every interval. INSTALMENT splits one amount
    into a fixed set of scheduled invoices.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    kind: str = ScheduleKind.RECURRING
    client_id: int = Field(foreign_key="client.id", index=True)
    description: str = ""
    amount_cents: int
    currency: str = "USD"
    terms_days: int = 14
    interval_unit: str = IntervalUnit.MONTH
    interval_count: int = 1
    # Recurring only: how many more invoices to queue. None repeats until cancelled.
    cycles_left: Optional[int] = None
    # Recurring only: the date of the invoice that started the repeat. Each
    # occurrence counts from it, so a repeat from the 31st keeps the month end
    # (issue #43). None on a repeat an older build made.
    starts_on: Optional[datetime] = Field(default=None, sa_type=UTCDateTime)
    status: str = ScheduleStatus.ACTIVE
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class ScheduledInvoice(SQLModel, table=True):
    """One future invoice. UPCOMING, then PENDING approval, then SENT."""

    # One item per place in a schedule, even if two scans run at once (issue #40).
    __table_args__ = (Index("ux_scheduledinvoice_schedule_sequence", "schedule_id", "sequence", unique=True),)

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    schedule_id: int = Field(foreign_key="schedule.id", index=True)
    sequence: int = 1
    amount_cents: int
    description: str = ""
    issue_on: datetime = Field(sa_type=UTCDateTime)
    status: str = ScheduledInvoiceStatus.UPCOMING
    invoice_id: Optional[int] = Field(default=None, foreign_key="invoice.id")
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)


class RiskComparison(SQLModel, table=True):
    """One refit of logistic regression against the rule baseline (issue #22)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=utcnow, sa_type=UTCDateTime)
    status: str = ComparisonStatus.NOT_ENOUGH_HISTORY
    rows_used: int = 0
    late_count: int = 0
    test_rows: int = 0
    baseline_accuracy: Optional[float] = None
    baseline_auc: Optional[float] = None
    logistic_accuracy: Optional[float] = None
    logistic_auc: Optional[float] = None
    message: str = ""


def make_engine(database_url: str):
    connect_args = {}
    kwargs = {}
    if database_url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        if database_url in {"sqlite://", "sqlite:///:memory:"}:
            kwargs["poolclass"] = StaticPool
    return create_engine(database_url, connect_args=connect_args, **kwargs)


def init_db(engine) -> None:
    SQLModel.metadata.create_all(engine)
    add_missing_columns(engine)
    sync_indexes(engine)
    with engine.begin() as connection:
        seed_invoice_counter(connection)
        fill_session_keys(connection)


def fill_session_keys(connection) -> None:
    """Give each user without a session key a new one (issue #142).

    Users from an older build have none, so their cookies, which carry no
    key, stop working once and they sign in again.
    """
    rows = connection.execute(text('SELECT id FROM "user" WHERE session_key IS NULL')).all()
    for (user_id,) in rows:
        connection.execute(
            text('UPDATE "user" SET session_key = :key WHERE id = :id'), {"key": new_session_key(), "id": user_id}
        )


NUMBER_SUFFIX = re.compile(r"-(\d+)$")


def seed_invoice_counter(connection) -> None:
    """Add the counter row if it is missing, above every invoice so far (issue #114).

    It starts at the highest invoice id or the highest numeric suffix of an
    invoice number, whichever is larger, so a database from an older build
    never hands out a number it already holds. An existing row is left alone.
    """
    if connection.execute(text("SELECT 1 FROM invoicenumbercounter WHERE id = 1")).first():
        return
    highest = connection.execute(text("SELECT COALESCE(MAX(id), 0) FROM invoice")).scalar_one()
    for (number,) in connection.execute(text("SELECT invoice_number FROM invoice")):
        match = NUMBER_SUFFIX.search(number or "")
        if match:
            highest = max(highest, int(match.group(1)))
    connection.execute(
        text("INSERT OR IGNORE INTO invoicenumbercounter (id, last) VALUES (1, :last)"), {"last": highest}
    )


def sync_indexes(engine) -> None:
    """Create indexes a newer model declares, and fix ones whose uniqueness changed.

    Issue #24 made client email unique per user instead of globally, so an
    older database must lose its global unique index on client.email.

    A unique index cannot be built over rows that already break it, such as
    two pending drafts for one invoice left by the race issue #40 closes. The
    rows are left alone, a warning names the index, and the next startup tries
    again. Startup never fails on it.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {index["name"]: bool(index["unique"]) for index in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name in present and present[index.name] == bool(index.unique):
                continue
            try:
                with engine.begin() as connection:
                    if index.name in present:
                        connection.execute(text(f'DROP INDEX "{index.name}"'))
                    index.create(connection)
            except IntegrityError:
                log.warning(
                    "Could not add unique index %s: table %s already holds rows that break it. "
                    "Resolve them and restart. See docs/how-to/troubleshoot.md.",
                    index.name,
                    table.name,
                )


def add_missing_columns(engine) -> None:
    """Add columns that a newer model declares to tables an older build created.

    create_all only creates missing tables. A demo database from an earlier
    build would otherwise fail on the first query that reads a new column.
    Only additive changes are handled; nothing is renamed or dropped.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                column_type = column.type.compile(dialect=engine.dialect)
                default = _literal_default(column)
                clause = f" DEFAULT {default}" if default is not None else ""
                connection.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column_type}{clause}')
                )


def _literal_default(column) -> str | None:
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None
