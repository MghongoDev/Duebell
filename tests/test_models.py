"""Stored times read back as aware UTC, whatever sqlmodel version is installed (issue #34)."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlmodel import Session

from app.models import Client, Invoice, init_db, make_engine, utcnow


def _engine():
    engine = make_engine("sqlite://")
    init_db(engine)
    return engine


def test_a_naive_stored_time_reads_back_as_utc():
    engine = _engine()
    with Session(engine) as session:
        session.add(Client(id=1, name="Acme", email="ap@acme.test"))
        session.add(Invoice(id=1, client_id=1, invoice_number="DUE-1", amount_cents=100))
        session.commit()
        # The shape SQLite holds: no offset.
        session.exec(text("UPDATE invoice SET due_at = '2026-01-15 09:00:00.000000' WHERE id = 1"))
        session.commit()
        session.expire_all()
        invoice = session.get(Invoice, 1)

    assert invoice.due_at == datetime(2026, 1, 15, 9, tzinfo=timezone.utc)
    assert invoice.due_at.tzinfo is not None
    assert invoice.due_at < utcnow()


def test_an_aware_time_in_another_zone_is_stored_as_utc():
    engine = _engine()
    nairobi = timezone(timedelta(hours=3))
    with Session(engine) as session:
        client = Client(name="Acme", email="ap@acme.test", created_at=datetime(2026, 1, 1, 12, tzinfo=nairobi))
        session.add(client)
        session.commit()
        client_id = client.id
    with Session(engine) as session:
        stored = session.get(Client, client_id).created_at

    assert stored == datetime(2026, 1, 1, 9, tzinfo=timezone.utc)
    assert stored.utcoffset() == timedelta(0)
