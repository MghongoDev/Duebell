"""Three clean reset-to-demo runs (issue #18).

Each cycle deletes a scratch database, seeds it, scans for reminders, and checks
that every draft is pending. Nothing is sent. Run with:

    .venv/bin/python scripts/smoke.py
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, select

from app.config import get_settings
from app.models import Client, Invoice, Reminder, init_db, make_engine
from app.paypal import MockPayPal
from app.reminders import draft_reminders
from scripts.seed import reset_database, seed

SCRATCH_DB = "sqlite:////tmp/duebell-smoke.db"
EXPECTED_CLIENTS = 6
EXPECTED_INVOICES = 26


def one_cycle(settings) -> dict:
    reset_database(settings)
    engine = make_engine(settings.database_url)
    init_db(engine)
    gateway = MockPayPal()
    with Session(engine) as session:
        summary = seed(session)
        created = draft_reminders(session, settings)
        reminders = session.exec(select(Reminder)).all()
        clients = len(session.exec(select(Client)).all())
        invoices = len(session.exec(select(Invoice)).all())

    assert summary == {"clients": EXPECTED_CLIENTS, "invoices": EXPECTED_INVOICES}
    assert clients == EXPECTED_CLIENTS
    assert invoices == EXPECTED_INVOICES
    assert created, "the seeded demo should produce at least one draft"
    assert all(item.status == "PENDING" for item in reminders)
    assert gateway.sent_reminders == []
    return {"clients": clients, "invoices": invoices, "drafts": len(created)}


def main() -> None:
    settings = replace(get_settings(), database_url=SCRATCH_DB)
    try:
        for cycle in range(1, 4):
            summary = one_cycle(settings)
            print(
                f"Cycle {cycle}: {summary['clients']} clients, "
                f"{summary['invoices']} invoices, {summary['drafts']} pending drafts, 0 sent"
            )
        print("Smoke: pass")
    finally:
        reset_database(settings)


if __name__ == "__main__":
    main()
