"""Seed synthetic clients and invoices for the Duebell demo (issue #13)."""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session

from app.config import get_settings
from app.models import Client, Invoice, init_db, make_engine, utcnow
from app.risk import HistoryInvoice, score_invoice

CLIENTS = [
    ("Northwind Studio", "accounts@northwind.test", 0, 4, 85000, 12),
    ("Acme Studio", "billing@acme.test", 9, 4, 120000, -6),
    ("Blue Harbor", "pay@blueharbor.test", 4, 4, 240000, 20),
    ("Cedar and Co", "hello@cedar.test", 1, 4, 45000, 9),
    ("Redwood Labs", "ap@redwood.test", 14, 4, 310000, -2),
    ("Lumen Works", "finance@lumen.test", None, 0, 95000, 14),
]

DESCRIPTIONS = [
    "Brand identity and logo design",
    "Website redesign, phase one",
    "Monthly retainer, October",
    "Product photography",
    "Copywriting for launch page",
    "Design system audit",
]


def reset_database(settings) -> None:
    if settings.database_url.startswith("sqlite:///"):
        path = Path(settings.database_url.replace("sqlite:///", ""))
        if path.exists():
            path.unlink()


def seed(session: Session) -> dict:
    now = utcnow()
    counter = 0
    total_invoices = 0

    for index, (name, email, habit, history_count, amount, open_due_offset) in enumerate(CLIENTS):
        client = Client(name=name, email=email)
        session.add(client)
        session.commit()
        session.refresh(client)

        prior: list[HistoryInvoice] = []
        for step in range(history_count):
            counter += 1
            issued = now - timedelta(days=150 - step * 28)
            due = issued + timedelta(days=30)
            paid = due + timedelta(days=(habit or 0) + (step % 3 if habit else 0))
            invoice = Invoice(
                client_id=client.id,
                invoice_number=f"DUE-SEED-{counter:04d}",
                description=DESCRIPTIONS[(index + step) % len(DESCRIPTIONS)],
                amount_cents=amount,
                currency="USD",
                terms_days=30,
                issued_at=issued,
                due_at=due,
                paid_at=paid,
                status="PAID",
                risk_score=None,
                risk_level=None,
                risk_reasons="",
            )
            session.add(invoice)
            prior.append(HistoryInvoice(amount_cents=amount, due_at=due, paid_at=paid))
            total_invoices += 1

        session.commit()

        counter += 1
        due = now + timedelta(days=open_due_offset)
        assessment = score_invoice(amount, 14, prior)
        invoice = Invoice(
            client_id=client.id,
            invoice_number=f"DUE-SEED-{counter:04d}",
            description=DESCRIPTIONS[index % len(DESCRIPTIONS)],
            amount_cents=amount,
            currency="USD",
            terms_days=14,
            issued_at=now - timedelta(days=7),
            due_at=due,
            status="SENT",
            risk_score=assessment.score,
            risk_level=assessment.level,
            risk_reasons="\n".join(assessment.reasons),
        )
        session.add(invoice)
        total_invoices += 1

    session.commit()
    return {"clients": len(CLIENTS), "invoices": total_invoices}


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Duebell demo database.")
    parser.add_argument("--reset", action="store_true", help="Delete the database first.")
    args = parser.parse_args()

    settings = get_settings()
    if args.reset:
        reset_database(settings)
    engine = make_engine(settings.database_url)
    init_db(engine)
    with Session(engine) as session:
        summary = seed(session)
    print(f"Seeded {summary['clients']} clients and {summary['invoices']} invoices.")
    print("Training data is synthetic. The risk model is a demonstration, not a validated predictor.")


if __name__ == "__main__":
    main()
