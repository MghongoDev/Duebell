"""Database models and engine setup for Duebell (issue #9)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

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


class Event(SQLModel, table=True):
    paypal_event_id: str = Field(primary_key=True)
    event_type: str
    payload: str = ""
    received_at: datetime = Field(default_factory=utcnow)


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
