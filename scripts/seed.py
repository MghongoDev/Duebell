"""Seed synthetic clients and invoices for the Duebell demo (issues #13, #24, #86; ADR 0008).

The seed belongs to a demo user. --if-empty seeds only a database with no
users, and --require-password refuses the published default password, which
is how a hosted instance starts. A bare rerun on a seeded database changes
nothing, and an unusable DEMO_PASSWORD exits with one line, not a traceback
(issue #86). --reset checks the password before it deletes anything, so a
bad one leaves the data in place (issue #102). It then empties every table in
place rather than deleting the file, so a running app keeps working and sees
the new data (issue #121).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, SQLModel, select

from app.auth import AuthError, create_user, validate_password
from app.config import get_settings
from app.models import Client, Invoice, InvoiceStatus, User, init_db, make_engine, utcnow
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


def empty_database(engine) -> None:
    """Delete every row of every table in one transaction, keeping the file (issue #121).

    A running app keeps its connection on the same file, so it goes on
    writing and sees the reseeded data. Deleting the file would leave it on a
    deleted, read-only copy until restart. Children go before parents, so no
    row points at a deleted one.
    """
    with engine.begin() as connection:
        for table in reversed(SQLModel.metadata.sorted_tables):
            connection.execute(table.delete())


DEMO_EMAIL = "demo@duebell.test"
DEFAULT_DEMO_PASSWORD = "duebell-demo-2026"


def demo_user(session: Session, require_password: bool = False) -> User:
    """The demo account. A hosted instance must set DEMO_PASSWORD.

    The default password is published in the README, so require_password
    refuses to fall back to it.
    """
    user = session.exec(select(User).where(User.email == DEMO_EMAIL)).first()
    if user:
        return user
    return create_user(session, DEMO_EMAIL, demo_password(require_password))


def demo_password(require_password: bool = False) -> str:
    """DEMO_PASSWORD, or the default if unset. Raise before anything is written if it is unusable (issue #102).

    Exits if require_password is set and DEMO_PASSWORD is unset, and raises
    AuthError if create_user would refuse the password.
    """
    password = os.getenv("DEMO_PASSWORD", "")
    if not password:
        if require_password:
            raise SystemExit("Set DEMO_PASSWORD. A hosted demo must not use the published default.")
        password = DEFAULT_DEMO_PASSWORD
    validate_password(password)
    return password


def seed_if_empty(session: Session, require_password: bool = False) -> dict | None:
    """Seed only a database with no users, so a restart never wipes real data."""
    if session.exec(select(User)).first() is not None:
        return None
    return seed(session, demo_user(session, require_password).id)


def already_seeded(session: Session) -> bool:
    """True once the demo data is in, so a rerun can stop before it writes (issue #86).

    Seed invoice numbers are unique across the database, and seed clients are
    unique per demo user, so either one present means a second seed would fail.
    """
    if session.exec(select(Invoice).where(Invoice.invoice_number.startswith("DUE-SEED-"))).first():
        return True
    user = session.exec(select(User).where(User.email == DEMO_EMAIL)).first()
    if user is None:
        return False
    emails = [email for _, email, *_ in CLIENTS]
    return session.exec(select(Client).where(Client.user_id == user.id, Client.email.in_(emails))).first() is not None


def seed(session: Session, user_id: int | None = None) -> dict:
    """Write the six clients and their invoices in one commit, so a failure leaves none (issue #86)."""
    if user_id is None:
        user_id = demo_user(session).id
    now = utcnow()
    counter = 0
    total_invoices = 0

    for index, (name, email, habit, history_count, amount, open_due_offset) in enumerate(CLIENTS):
        client = Client(name=name, email=email, user_id=user_id)
        session.add(client)
        session.flush()

        prior: list[HistoryInvoice] = []
        for step in range(history_count):
            counter += 1
            issued = now - timedelta(days=150 - step * 28)
            due = issued + timedelta(days=30)
            paid = due + timedelta(days=(habit or 0) + (step % 3 if habit else 0))
            invoice = Invoice(
                user_id=user_id,
                client_id=client.id,
                invoice_number=f"DUE-SEED-{counter:04d}",
                description=DESCRIPTIONS[(index + step) % len(DESCRIPTIONS)],
                amount_cents=amount,
                currency="USD",
                terms_days=30,
                issued_at=issued,
                due_at=due,
                paid_at=paid,
                status=InvoiceStatus.PAID,
                risk_score=None,
                risk_level=None,
                risk_reasons="",
            )
            session.add(invoice)
            prior.append(HistoryInvoice(amount_cents=amount, due_at=due, paid_at=paid))
            total_invoices += 1

        counter += 1
        due = now + timedelta(days=open_due_offset)
        assessment = score_invoice(amount, 14, prior)
        invoice = Invoice(
            user_id=user_id,
            client_id=client.id,
            invoice_number=f"DUE-SEED-{counter:04d}",
            description=DESCRIPTIONS[index % len(DESCRIPTIONS)],
            amount_cents=amount,
            currency="USD",
            terms_days=14,
            issued_at=now - timedelta(days=7),
            due_at=due,
            status=InvoiceStatus.SENT,
            risk_score=assessment.score,
            risk_level=assessment.level,
            risk_reasons="\n".join(assessment.reasons),
            limited_data=assessment.limited_data,
            limited_data_reason=assessment.limited_data_reason,
        )
        session.add(invoice)
        total_invoices += 1

    session.commit()
    return {"clients": len(CLIENTS), "invoices": total_invoices}


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Duebell demo database.")
    parser.add_argument("--reset", action="store_true", help="Empty every table first.")
    parser.add_argument("--if-empty", action="store_true", help="Seed only if the database has no users.")
    parser.add_argument(
        "--require-password", action="store_true", help="Refuse to use the default demo password."
    )
    args = parser.parse_args()

    settings = get_settings()
    try:
        if args.reset:
            # Check the password before the delete, so a bad one leaves the data in place (issue #102).
            demo_password(args.require_password)
        engine = make_engine(settings.database_url)
        init_db(engine)
        if args.reset:
            # Empty the tables, not the file, so a running app keeps working (issue #121).
            empty_database(engine)
            print("Emptied every table in place. A running app keeps working and sees the new data.")
        with Session(engine) as session:
            if args.if_empty:
                summary = seed_if_empty(session, args.require_password)
                if summary is None:
                    print("The database already has users. Nothing seeded.")
                    return
            elif already_seeded(session):
                print("The demo data is already seeded. Nothing changed. Use --reset to start over.")
                return
            else:
                summary = seed(session, demo_user(session, args.require_password).id)
    except AuthError as error:
        # The password is checked before any delete or write, so nothing is lost or left behind.
        raise SystemExit(f"DEMO_PASSWORD is not usable: {error}") from None
    print(f"Seeded {summary['clients']} clients and {summary['invoices']} invoices.")
    print(f"Sign in as {DEMO_EMAIL}. The password is DEMO_PASSWORD, or {DEFAULT_DEMO_PASSWORD} if unset.")
    print("Training data is synthetic. The risk model is a demonstration, not a validated predictor.")


if __name__ == "__main__":
    main()
