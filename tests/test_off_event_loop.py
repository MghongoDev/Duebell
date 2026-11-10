"""Slow calls run off the event loop, and the routes that now run in parallel act once (issue #48)."""

import threading
import time

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import services
from app.config import Settings
from app.models import Event, Invoice, Reminder, make_engine
from app.paypal import PayPalError
from app.reminders import draft_reminders
from app.web import create_app
from tests.conftest import PASSWORD, sign_in
from tests.test_reminders import seed_invoice
from tests.test_webhooks import create, event_body

PAUSE = 3  # seconds a slow call waits unless the test lets it go


def file_app(tmp_path):
    """An app over a file database, so threads each get their own connection."""
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'race.db'}",
        mock_webhooks=True,
        amount_cap_cents=1_000_000,
        allowed_currencies=("USD",),
        terms_min_days=0,
        terms_max_days=120,
    )
    return create_app(settings=settings, engine=make_engine(settings.database_url))


def second_tab(app):
    tab = TestClient(app)
    tab.post("/login", data={"email": "owner@duebell.test", "password": PASSWORD})
    return tab


def pending_reminder_id(client):
    seed_invoice(client)
    with Session(client.app.state.engine) as session:
        return draft_reminders(session, client.app.state.settings, scope=client.user_id)[0].id


def healthz_while(client, slow_request, inside):
    """Start slow_request in a thread, then time /healthz while it is stuck.

    The client is used as a context manager, so every request shares one
    event loop, as they do under uvicorn.
    """
    results = {}
    worker = threading.Thread(target=lambda: results.setdefault("slow", slow_request()))
    worker.start()
    assert inside.wait(timeout=PAUSE)
    started = time.monotonic()
    health = client.get("/healthz")
    elapsed = time.monotonic() - started
    worker.join(timeout=PAUSE * 2)
    assert health.status_code == 200
    return elapsed, results["slow"]


def test_health_check_answers_while_a_slow_parse_runs(app):
    inside, let_go = threading.Event(), threading.Event()
    real_parse = app.state.llm

    def slow_parse(text):
        inside.set()
        let_go.wait(timeout=PAUSE)
        return real_parse(text)

    app.state.llm = slow_parse
    with TestClient(app) as client:
        sign_in(client)
        try:
            elapsed, parsed = healthz_while(
                client,
                lambda: client.post("/parse", data={"description": "Invoice Acme Studio, billing@acme.test, 1200 USD for a logo, net 14 days."}),
                inside,
            )
        finally:
            let_go.set()
    assert elapsed < 1, f"/healthz waited {elapsed:.1f}s behind the parse"
    assert parsed.status_code == 200


def test_health_check_answers_while_a_reminder_approval_waits_on_paypal(app):
    inside, let_go = threading.Event(), threading.Event()
    gateway = app.state.gateway
    real_send = gateway.send_reminder

    def slow_send(*args, **kwargs):
        inside.set()
        let_go.wait(timeout=PAUSE)
        return real_send(*args, **kwargs)

    with TestClient(app) as client:
        sign_in(client)
        reminder_id = pending_reminder_id(client)
        gateway.send_reminder = slow_send
        try:
            elapsed, approved = healthz_while(
                client, lambda: client.post(f"/api/reminders/{reminder_id}/approve"), inside
            )
        finally:
            let_go.set()
    assert elapsed < 1, f"/healthz waited {elapsed:.1f}s behind the approval"
    assert approved.status_code == 200


def test_health_check_answers_while_a_webhook_waits_on_paypal(app):
    inside, let_go = threading.Event(), threading.Event()
    gateway = app.state.gateway
    real_verify = gateway.verify_webhook

    def slow_verify(*args, **kwargs):
        inside.set()
        let_go.wait(timeout=PAUSE)
        return real_verify(*args, **kwargs)

    with TestClient(app) as client:
        sign_in(client)
        row = create(client)
        gateway.verify_webhook = slow_verify
        body = event_body("WH-SLOW", "INVOICING.INVOICE.PAID", row["paypal_invoice_id"])
        try:
            elapsed, hooked = healthz_while(client, lambda: client.post("/webhooks/paypal", content=body), inside)
        finally:
            let_go.set()
    assert elapsed < 1, f"/healthz waited {elapsed:.1f}s behind the webhook"
    assert hooked.status_code == 200


def test_two_reminder_approvals_at_once_send_one_reminder(tmp_path):
    app = file_app(tmp_path)
    first_tab = sign_in(TestClient(app))
    other_tab = second_tab(app)
    reminder_id = pending_reminder_id(first_tab)

    gateway = app.state.gateway
    real_send = gateway.send_reminder
    inside_paypal, let_go = threading.Event(), threading.Event()

    def slow_send(*args, **kwargs):
        inside_paypal.set()
        let_go.wait(timeout=PAUSE)
        return real_send(*args, **kwargs)

    gateway.send_reminder = slow_send
    results = {}
    worker = threading.Thread(
        target=lambda: results.setdefault("first", first_tab.post(f"/api/reminders/{reminder_id}/approve"))
    )
    worker.start()
    assert inside_paypal.wait(timeout=PAUSE)
    second = other_tab.post(f"/api/reminders/{reminder_id}/approve")
    let_go.set()
    worker.join(timeout=PAUSE * 2)

    assert second.status_code == 409
    assert results["first"].status_code == 200
    assert results["first"].json()["status"] == "SENT"
    assert len(gateway.sent_reminders) == 1


def test_a_failed_send_releases_the_claim(client):
    reminder_id = pending_reminder_id(client)
    gateway = client.app.state.gateway
    real_send = gateway.send_reminder

    def fail(*args, **kwargs):
        raise PayPalError("PayPal is down.")

    gateway.send_reminder = fail
    assert client.post(f"/api/reminders/{reminder_id}/approve").status_code == 502
    assert _status(client, reminder_id) == "PENDING"

    gateway.send_reminder = real_send
    response = client.post(f"/api/reminders/{reminder_id}/approve")
    assert response.status_code == 200
    assert len(gateway.sent_reminders) == 1


def test_a_reminder_approval_a_restart_cut_short_is_pending_again(client, settings):
    reminder_id = pending_reminder_id(client)
    engine = client.app.state.engine
    with Session(engine) as session:
        reminder = session.get(Reminder, reminder_id)
        reminder.status = "SENDING"
        session.add(reminder)
        session.commit()

    create_app(settings=settings, engine=engine)
    assert _status(client, reminder_id) == "PENDING"


def test_a_scan_drafts_nothing_for_an_invoice_whose_reminder_is_sending(client):
    reminder_id = pending_reminder_id(client)
    with Session(client.app.state.engine) as session:
        reminder = session.get(Reminder, reminder_id)
        reminder.status = "SENDING"
        session.add(reminder)
        session.commit()
        assert draft_reminders(session, client.app.state.settings, scope=client.user_id) == []


def test_two_copies_of_one_webhook_at_once_apply_once(tmp_path, monkeypatch):
    app = file_app(tmp_path)
    client = sign_in(TestClient(app, raise_server_exceptions=False))
    row = create(client)
    body = event_body("WH-TWICE", "INVOICING.INVOICE.PAID", row["paypal_invoice_id"])

    real_find = services.find_by_paypal_id
    inside, let_go = threading.Event(), threading.Event()

    def slow_find(*args, **kwargs):
        if not inside.is_set():
            inside.set()
            let_go.wait(timeout=PAUSE)
        return real_find(*args, **kwargs)

    monkeypatch.setattr(services, "find_by_paypal_id", slow_find)
    results = {}
    first = threading.Thread(target=lambda: results.setdefault("first", client.post("/webhooks/paypal", content=body)))
    first.start()
    assert inside.wait(timeout=PAUSE)
    second = threading.Thread(
        target=lambda: results.setdefault("second", client.post("/webhooks/paypal", content=body))
    )
    second.start()
    second.join(timeout=0.5)  # give the copy time to run, or to wait its turn
    let_go.set()
    first.join(timeout=PAUSE * 2)
    second.join(timeout=PAUSE * 2)

    answers = sorted((r.status_code, bool(r.json().get("duplicate"))) for r in results.values())
    assert answers == [(200, False), (200, True)]
    with Session(app.state.engine) as session:
        assert len(session.exec(select(Event)).all()) == 1
        [invoice] = session.exec(select(Invoice)).all()
        assert invoice.status == "PAID"


def _status(client, reminder_id):
    with Session(client.app.state.engine) as session:
        return session.get(Reminder, reminder_id).status

