"""The services module follows up a sent invoice without anyone importing schedules first (issue #103).

pytest has already imported app.schedules by the time this file runs, so the
check runs in a fresh Python process that imports only app.services and the
models.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCRIPT = textwrap.dedent(
    """
    import sys

    from sqlmodel import Session, select

    from app.models import Client, Invoice, Schedule, User, init_db, make_engine
    from app.services import refresh_invoice

    assert "app.schedules" not in sys.modules, "this check must start without app.schedules"


    class SentGateway:
        def get_invoice(self, paypal_invoice_id):
            return "SENT"


    engine = make_engine("sqlite://")
    init_db(engine)
    with Session(engine) as session:
        user = User(email="owner@duebell.test", password_hash="x")
        session.add(user)
        session.commit()
        client = Client(user_id=user.id, name="Acme", email="ap@acme.test")
        session.add(client)
        session.commit()
        invoice = Invoice(
            user_id=user.id,
            client_id=client.id,
            invoice_number="INV-103",
            paypal_invoice_id="PP-103",
            amount_cents=50_000,
            repeat_unit="MONTH",
            repeat_cycles=2,
        )
        session.add(invoice)
        session.commit()

        invoice = refresh_invoice(session, invoice, SentGateway())

        assert invoice.status == "SENT"
        schedules = session.exec(select(Schedule)).all()
        assert len(schedules) == 1, f"expected one schedule, found {len(schedules)}"
        assert schedules[0].user_id == user.id
        assert invoice.repeat_unit is None, "the hold is used once"
    print("ok")
    """
)


def test_a_refresh_starts_the_held_repeat_when_only_services_is_imported():
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
