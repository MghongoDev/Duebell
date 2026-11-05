"""Database models and engine setup for Duebell (issues #9, #22, #24, #25, #26).

Every table that holds a user's data has a user_id (ADR 0008). init_db adds
columns and indexes a newer model declares to an older database, so a demo
database keeps working across builds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Index, inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Field, SQLModel, create_engine


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Treat a naive datetime from storage as UTC, so it compares with utcnow()."""
    if value is None or value.tzinfo:
        return value
    return value.replace(tzinfo=timezone.utc)


class User(SQLModel, table=True):
    """A person who signs in. Every record below belongs to one user (issue #24)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str
    created_at: datetime = Field(default_factory=utcnow)


class Client(SQLModel, table=True):
    # Two users may bill the same client email, so email is unique per user.
    __table_args__ = (Index("ux_client_user_email", "user_id", "email", unique=True),)

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    name: str
    email: str = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)


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
    issued_at: datetime = Field(default_factory=utcnow)
    due_at: datetime = Field(default_factory=utcnow)
    paid_at: Optional[datetime] = None
    status: str = "DRAFT"
    risk_score: Optional[float] = None
    risk_level: Optional[str] = None
    risk_reasons: str = ""
    limited_data: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class Reminder(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    invoice_id: int = Field(foreign_key="invoice.id", index=True)
    draft_text: str
    tone: str = "friendly"
    status: str = "DRAFT"
    created_at: datetime = Field(default_factory=utcnow)
    sent_at: Optional[datetime] = None
    # Comma-separated channels that have already delivered this reminder,
    # e.g. "paypal" or "paypal,email". A retry skips them.
    sent_channels: str = ""


class Event(SQLModel, table=True):
    paypal_event_id: str = Field(primary_key=True)
    # The owner of the invoice the event touched. None if it matched no invoice.
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    event_type: str
    payload: str = ""
    received_at: datetime = Field(default_factory=utcnow)


class Schedule(SQLModel, table=True):
    """A rule that produces future invoices (issue #25).

    RECURRING repeats one amount every interval. INSTALMENT splits one amount
    into a fixed set of scheduled invoices.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    kind: str = "RECURRING"
    client_id: int = Field(foreign_key="client.id", index=True)
    description: str = ""
    amount_cents: int
    currency: str = "USD"
    terms_days: int = 14
    interval_unit: str = "MONTH"
    interval_count: int = 1
    # Recurring only: how many more invoices to queue. None repeats until cancelled.
    cycles_left: Optional[int] = None
    status: str = "ACTIVE"
    created_at: datetime = Field(default_factory=utcnow)


class ScheduledInvoice(SQLModel, table=True):
    """One future invoice. UPCOMING, then PENDING approval, then SENT."""

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    schedule_id: int = Field(foreign_key="schedule.id", index=True)
    sequence: int = 1
    amount_cents: int
    description: str = ""
    issue_on: datetime
    status: str = "UPCOMING"
    invoice_id: Optional[int] = Field(default=None, foreign_key="invoice.id")
    created_at: datetime = Field(default_factory=utcnow)


class RiskComparison(SQLModel, table=True):
    """One refit of logistic regression against the rule baseline (issue #22)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    status: str = "NOT_ENOUGH_HISTORY"
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


def sync_indexes(engine) -> None:
    """Create indexes a newer model declares, and fix ones whose uniqueness changed.

    Issue #24 made client email unique per user instead of globally, so an
    older database must lose its global unique index on client.email.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {index["name"]: bool(index["unique"]) for index in inspector.get_indexes(table.name)}
            for index in table.indexes:
                if index.name in present and present[index.name] != bool(index.unique):
                    connection.execute(text(f'DROP INDEX "{index.name}"'))
                    present.pop(index.name)
                if index.name not in present:
                    index.create(connection)


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
