"""Three clean reset-to-demo runs (issue #18).

Each cycle resets and seeds a scratch database, then walks the demo through the
HTTP routes with the mock adapters: parse the demo sentence, create the invoice,
scan, approve the reminder, simulate payment, and replay the paid webhook. Run:

    .venv/bin/python scripts/smoke.py
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Client, Invoice, init_db, make_engine
from app.paypal import MockPayPal
from app.web import create_app
from scripts.seed import DEFAULT_DEMO_PASSWORD, DEMO_EMAIL, reset_database, seed

SCRATCH_DB = "sqlite:////tmp/duebell-smoke.db"
EXPECTED_CLIENTS = 6
EXPECTED_INVOICES = 26
DEMO_SENTENCE = "Invoice Acme Studio, billing@acme.test, 1200 USD for logo design, net 2 days."


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"Smoke: FAIL. {message}")


def one_cycle(settings) -> dict:
    reset_database(settings)
    engine = make_engine(settings.database_url)
    init_db(engine)
    with Session(engine) as session:
        summary = seed(session)
    check(summary == {"clients": EXPECTED_CLIENTS, "invoices": EXPECTED_INVOICES}, f"seed gave {summary}")

    gateway = MockPayPal()
    client = TestClient(create_app(settings=settings, engine=engine, gateway=gateway))
    signed_in = client.post(
        "/login", data={"email": DEMO_EMAIL, "password": DEFAULT_DEMO_PASSWORD}, follow_redirects=False
    )
    check(signed_in.status_code == 303, f"demo sign-in returned {signed_in.status_code}")
    check(client.get("/api/invoices").status_code == 200, "the demo user cannot read invoices")

    parsed = client.post("/parse", data={"description": DEMO_SENTENCE}).json()
    check(parsed.get("ok") is True, f"parse failed: {parsed}")
    created = client.post("/invoices", data=parsed["proposal"], follow_redirects=False)
    check(created.status_code == 303, f"create returned {created.status_code}")

    with Session(engine) as session:
        check(len(session.exec(select(Client)).all()) == EXPECTED_CLIENTS, "the demo client was duplicated")
        invoice = session.exec(select(Invoice).where(Invoice.paypal_invoice_id.is_not(None))).one()
    check(invoice.status == "SENT", f"new invoice is {invoice.status}")

    scan = client.post("/api/reminders/scan").json()
    check(scan.get("created") == 1, f"scan drafted {scan.get('created')} reminders, expected 1")
    pending = [row for row in client.get("/api/reminders").json() if row["status"] == "PENDING"]
    check(len(pending) == 1, f"{len(pending)} pending drafts")
    check(gateway.sent_reminders == [], "a reminder was sent before approval")
    approved = client.post(f"/api/reminders/{pending[0]['id']}/approve").json()
    check(
        approved == {"ok": True, "status": "SENT", "sent_channels": ["paypal"]},
        f"approve returned {approved}",
    )
    check(len(gateway.sent_reminders) == 1, "approval did not reach PayPal once")

    paid = client.post(f"/api/invoices/{invoice.id}/simulate-payment").json()
    check(paid == {"ok": True, "status": "PAID"}, f"simulate payment returned {paid}")

    event = json.dumps(
        {
            "id": "SMOKE-EVENT-1",
            "event_type": "INVOICING.INVOICE.PAID",
            "resource": {"id": invoice.paypal_invoice_id},
        }
    )
    first = client.post("/webhooks/paypal", content=event).json()
    replay = client.post("/webhooks/paypal", content=event).json()
    check(first.get("ok") is True and replay.get("duplicate") is True, f"webhook gave {first}, {replay}")
    refreshed = client.post(f"/api/invoices/{invoice.id}/refresh").json()
    check(refreshed == {"ok": True, "status": "PAID"}, f"refresh returned {refreshed}")

    engine.dispose()
    return {"invoice": invoice.invoice_number, "risk": invoice.risk_level}


def main() -> None:
    os.environ.pop("DEMO_PASSWORD", None)
    settings = replace(get_settings(), database_url=SCRATCH_DB, llm_mode="mock", paypal_mode="mock")
    try:
        for cycle in range(1, 4):
            summary = one_cycle(settings)
            print(
                f"Cycle {cycle}: created {summary['invoice']} ({summary['risk']} risk), "
                "reminder approved and sent once, paid, webhook replay ignored"
            )
        print("Smoke: pass")
    finally:
        reset_database(settings)


if __name__ == "__main__":
    main()
