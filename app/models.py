"""Database models and engine setup for Duebell (issue #9)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Field, SQLModel, create_engine


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Client(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    email: str = Field(index=True, unique=True)
    created_at: datetime = Field(default_factory=utcnow)


class Invoice(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
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
    event_type: str
    payload: str = ""
    received_at: datetime = Field(default_factory=utcnow)


class RiskComparison(SQLModel, table=True):
    """One refit of logistic regression against the rule baseline (issue #22)."""

    id: Optional[int] = Field(default=None, primary_key=True)
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
