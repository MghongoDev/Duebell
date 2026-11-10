"""Sign-in and per-user ownership (issue #24)."""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, select

from app import auth
from app.auth import (
    COOKIE_NAME,
    LOCKOUT_SECONDS,
    MAX_FAILURES,
    AuthError,
    Lockout,
    authenticate,
    create_user,
    make_session_cookie,
    read_session_cookie,
)
from app.models import Client, Event, Invoice, Reminder, Schedule, ScheduledInvoice, User, init_db, make_engine
from app.web import create_app
from scripts.create_user import claim_ownerless
from tests.conftest import PASSWORD, add_late_payer, sign_in

FORM = {
    "client_name": "Acme Studio",
    "client_email": "billing@acme.test",
    "description": "Logo design",
    "amount": "1200.00",
    "currency": "USD",
    "terms_days": "2",
}

PUBLIC = {"/login", "/healthz", "/webhooks/paypal", "/static", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def test_every_private_route_turns_away_an_anonymous_request(app):
    from app.web import ROUTERS

    anonymous = TestClient(app)
    checked = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if path in PUBLIC or path.startswith("/static"):
            continue
        url = path.replace("{invoice_id}", "1").replace("{reminder_id}", "1")
        url = url.replace("{item_id}", "1").replace("{schedule_id}", "1")
        for method in getattr(route, "methods", set()) - {"HEAD"}:
            response = anonymous.request(method, url, follow_redirects=False)
            # Pages and form posts send the person to sign in; the JSON
            # routes answer 401 (issue #84).
            if not url.startswith("/api/") and url != "/parse":
                assert response.status_code == 303 and response.headers["location"] == "/login", url
            else:
                assert response.status_code == 401, (method, url)
            checked.add((method, path))
    # Every route the route modules declare must be on the app and checked,
    # so a route lost in registration cannot slip past this test.
    declared = {
        (method, route.path)
        for module in ROUTERS
        for route in module.router.routes
        for method in route.methods - {"HEAD"}
        if route.path not in PUBLIC
    }
    assert declared and checked == declared


def test_public_routes_stay_open(app):
    anonymous = TestClient(app)
    assert anonymous.get("/healthz").status_code == 200
    assert anonymous.get("/login").status_code == 200
    assert anonymous.get("/static/styles.css").status_code == 200


def test_wrong_password_is_refused_and_repeated_failures_lock(app):
    client = TestClient(app)
    with Session(app.state.engine) as session:
        create_user(session, "owner@duebell.test", PASSWORD)
    for _ in range(MAX_FAILURES):
        response = client.post("/login", data={"email": "owner@duebell.test", "password": "wrong-password"})
        assert response.status_code == 401
        assert "do not match" in response.text
    locked = client.post("/login", data={"email": "owner@duebell.test", "password": PASSWORD})
    assert locked.status_code == 401
    assert "Too many failed attempts" in locked.text


def test_unknown_email_gets_the_same_message(app):
    response = TestClient(app).post("/login", data={"email": "nobody@duebell.test", "password": PASSWORD})
    assert response.status_code == 401
    assert "do not match" in response.text


def test_password_is_stored_hashed(app):
    with Session(app.state.engine) as session:
        user = create_user(session, "owner@duebell.test", PASSWORD)
        assert PASSWORD not in user.password_hash
        assert user.password_hash.startswith("scrypt$")
        with pytest.raises(AuthError, match="at least 10"):
            create_user(session, "short@duebell.test", "short")
        with pytest.raises(AuthError, match="already exists"):
            create_user(session, "OWNER@duebell.test", PASSWORD)


def test_session_cookie_is_signed_and_expires():
    cookie = make_session_cookie("secret", 7)
    assert read_session_cookie("secret", cookie) == 7
    assert read_session_cookie("other-secret", cookie) is None
    body, signature = cookie.rsplit(".", 1)
    assert read_session_cookie("secret", body + "." + signature[:-2] + "AA") is None
    assert read_session_cookie("secret", cookie, now=time.time() + 13 * 3600) is None
    assert read_session_cookie("secret", "garbage") is None


def test_sign_out_ends_the_session(client):
    assert client.get("/api/invoices").status_code == 200
    client.post("/logout", follow_redirects=False)
    client.cookies.clear()
    assert client.get("/api/invoices").status_code == 401


def expired_session(app) -> TestClient:
    """A client holding a session cookie past its 12-hour expiry."""
    signed_in = sign_in(TestClient(app), "expired@duebell.test")
    cookie = make_session_cookie(app.state.secret_key, signed_in.user_id, now=time.time() - 13 * 3600)
    return TestClient(app, cookies={COOKIE_NAME: cookie})


def test_expired_session_form_post_goes_to_sign_in(app):
    response = expired_session(app).post("/invoices", data=FORM, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_sign_out_clears_the_cookie_with_or_without_a_session(app, client):
    for who in (client, expired_session(app), TestClient(app)):
        response = who.post("/logout", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        header = response.headers["set-cookie"].lower()
        assert COOKIE_NAME in header and "max-age=0" in header


def test_expired_session_api_routes_still_answer_json(app):
    expired = expired_session(app)
    for method, url in (("GET", "/api/invoices"), ("POST", "/api/reminders/scan"), ("POST", "/parse")):
        response = expired.request(method, url, follow_redirects=False)
        assert response.status_code == 401, (method, url)
        assert response.json() == {"ok": False, "error": "Sign in first."}


def test_cookie_flags(app):
    with Session(app.state.engine) as session:
        create_user(session, "owner@duebell.test", PASSWORD)
    response = TestClient(app).post(
        "/login", data={"email": "owner@duebell.test", "password": PASSWORD}, follow_redirects=False
    )
    header = response.headers["set-cookie"].lower()
    assert COOKIE_NAME in header and "httponly" in header and "samesite=lax" in header


@pytest.fixture
def two_users(app):
    alice = sign_in(TestClient(app), "alice@duebell.test")
    bob = sign_in(TestClient(app), "bob@duebell.test")
    return alice, bob


def alice_records(alice):
    """Give Alice one of everything: an invoice, a reminder, a schedule and a pending instalment."""
    assert alice.post("/invoices", data=dict(FORM, repeat="MONTH"), follow_redirects=False).status_code == 303
    late = add_late_payer(alice)
    assert alice.post("/invoices", data=dict(FORM, **late, instalments="2"), follow_redirects=False).status_code == 303
    engine = alice.app.state.engine
    with Session(engine) as session:
        invoice = session.exec(
            select(Invoice).where(Invoice.user_id == alice.user_id, Invoice.description == FORM["description"])
        ).first()
        invoice.risk_level = "HIGH"
        session.add(invoice)
        session.commit()
        invoice_id = invoice.id
    assert alice.post("/api/reminders/scan").json()["created"] == 1
    with Session(engine) as session:
        reminder_id = session.exec(select(Reminder)).one().id
        schedule_id = session.exec(select(Schedule).where(Schedule.kind == "RECURRING")).one().id
        pending_id = session.exec(select(ScheduledInvoice).where(ScheduledInvoice.status == "PENDING")).one().id
    return {"invoice": invoice_id, "reminder": reminder_id, "schedule": schedule_id, "scheduled": pending_id}


def test_bob_sees_none_of_alices_records(two_users):
    alice, bob = two_users
    alice_records(alice)
    assert bob.get("/api/invoices").json() == []
    assert bob.get("/api/reminders").json() == []
    page = bob.get("/").text
    assert "DUE-" not in page and "instalment 1 of 2" not in page
    assert "DUE-" in alice.get("/").text
    assert bob.post("/api/risk/preview", data=FORM).json()["reasons"][0].startswith("New client")
    assert len(alice.get("/api/invoices").json()) == 5  # four paid earlier plus the repeat


@pytest.mark.parametrize(
    "path",
    [
        "/api/invoices/{invoice}/refresh",
        "/api/invoices/{invoice}/simulate-payment",
        "/api/reminders/{reminder}/approve",
        "/api/reminders/{reminder}/edit",
        "/api/reminders/{reminder}/reject",
        "/api/scheduled/{scheduled}/approve",
        "/api/scheduled/{scheduled}/reject",
        "/api/schedules/{schedule}/cancel",
    ],
)
def test_bob_cannot_act_on_alices_records(two_users, path):
    alice, bob = two_users
    ids = alice_records(alice)
    gateway = alice.app.state.gateway
    invoices_before = len(gateway.invoices)
    response = bob.post(path.format(**ids), data={"draft_text": "Pay now", "channel": "paypal"})
    assert response.status_code == 404
    assert gateway.sent_reminders == []
    assert len(gateway.invoices) == invoices_before
    with Session(alice.app.state.engine) as session:
        assert session.get(Invoice, ids["invoice"]).status == "SENT"
        assert session.get(Reminder, ids["reminder"]).status == "PENDING"
        assert session.get(Reminder, ids["reminder"]).draft_text != "Pay now"
        assert session.get(ScheduledInvoice, ids["scheduled"]).status == "PENDING"
        assert session.get(Schedule, ids["schedule"]).status == "ACTIVE"


def test_bobs_scan_and_refit_leave_alices_data_alone(two_users):
    alice, bob = two_users
    alice_records(alice)
    with Session(alice.app.state.engine) as session:
        for reminder in session.exec(select(Reminder)).all():
            session.delete(reminder)
        session.commit()
    assert bob.post("/api/reminders/scan").json() == {"ok": True, "created": 0, "scheduled": 0}
    assert bob.post("/api/risk/refit").json()["comparison"]["rows_used"] == 0


def test_webhook_applies_to_the_owner_and_only_they_see_it(two_users):
    alice, bob = two_users
    ids = alice_records(alice)
    with Session(alice.app.state.engine) as session:
        paypal_id = session.get(Invoice, ids["invoice"]).paypal_invoice_id
    anonymous = TestClient(alice.app)
    body = json.dumps({"id": "EV-1", "event_type": "INVOICING.INVOICE.PAID", "resource": {"id": paypal_id}})
    assert anonymous.post("/webhooks/paypal", content=body).json()["applied"] is True
    with Session(alice.app.state.engine) as session:
        assert session.get(Event, "EV-1").user_id == alice.user_id
    assert "INVOICING.INVOICE.PAID" in alice.get("/").text
    assert "INVOICING.INVOICE.PAID" not in bob.get("/").text


def test_two_users_can_bill_the_same_client_email(two_users):
    alice, bob = two_users
    assert alice.post("/invoices", data=FORM, follow_redirects=False).status_code == 303
    assert bob.post("/invoices", data=dict(FORM, client_name="Acme (Bob's)"), follow_redirects=False).status_code == 303
    with Session(alice.app.state.engine) as session:
        clients = session.exec(select(Client).where(Client.email == "billing@acme.test")).all()
    assert {client.user_id for client in clients} == {alice.user_id, bob.user_id}
    assert alice.get("/api/invoices").json()[0]["client_name"] == "Acme Studio"


def test_old_database_drops_global_email_uniqueness_and_can_be_claimed(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    old = create_engine(url)
    with old.begin() as connection:
        connection.execute(text("CREATE TABLE client (id INTEGER PRIMARY KEY, name VARCHAR, email VARCHAR, created_at DATETIME)"))
        connection.execute(text("CREATE UNIQUE INDEX ix_client_email ON client (email)"))
        connection.execute(text("INSERT INTO client (name, email) VALUES ('Acme', 'billing@acme.test')"))
    old.dispose()

    engine = make_engine(url)
    init_db(engine)
    indexes = {index["name"]: index["unique"] for index in inspect(engine).get_indexes("client")}
    assert not indexes["ix_client_email"]
    assert indexes["ux_client_user_email"]

    with Session(engine) as session:
        user = create_user(session, "owner@duebell.test", PASSWORD)
        assert claim_ownerless(session, user.id) == {"client": 1}
        assert session.exec(select(Client)).one().user_id == user.id
        session.add(Client(name="Acme for someone else", email="billing@acme.test", user_id=None))
        session.commit()


def test_seed_belongs_to_the_demo_user(tmp_path):
    from scripts.seed import DEMO_EMAIL, seed

    engine = make_engine(f"sqlite:///{tmp_path / 'demo.db'}")
    init_db(engine)
    with Session(engine) as session:
        seed(session)
        user = session.exec(select(User).where(User.email == DEMO_EMAIL)).one()
        owners = {invoice.user_id for invoice in session.exec(select(Invoice)).all()}
    assert owners == {user.id}


def test_seed_if_empty_keeps_existing_data(tmp_path, monkeypatch):
    from scripts.seed import seed_if_empty

    monkeypatch.setenv("DEMO_PASSWORD", "a-hosted-password")
    engine = make_engine(f"sqlite:///{tmp_path / 'host.db'}")
    init_db(engine)
    with Session(engine) as session:
        assert seed_if_empty(session)["invoices"] == 26
        session.add(User(email="real@duebell.test", password_hash="x"))
        session.commit()
        assert seed_if_empty(session) is None
        assert len(session.exec(select(Invoice)).all()) == 26


def test_hosted_seed_refuses_the_published_default_password(tmp_path, monkeypatch):
    from scripts.seed import demo_user

    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    engine = make_engine(f"sqlite:///{tmp_path / 'host.db'}")
    init_db(engine)
    with Session(engine) as session:
        with pytest.raises(SystemExit, match="DEMO_PASSWORD"):
            demo_user(session, require_password=True)


# The lockout under parallel attempts, its table size, and cross-site posts (issue #45).


def run_together(count, attempt):
    """Run `attempt(index)` on `count` threads released at the same moment."""
    barrier = threading.Barrier(count)
    results = [None] * count

    def worker(index):
        barrier.wait()
        results[index] = attempt(index)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


@pytest.fixture
def slow_checks(monkeypatch):
    """Count password checks and slow each one, so parallel attempts overlap."""
    calls = []
    real_check = auth.check_password

    def check(password, stored):
        calls.append(password)
        time.sleep(0.05)
        return real_check(password, stored)

    monkeypatch.setattr(auth, "check_password", check)
    return calls


def test_parallel_attempts_check_at_most_the_limit(tmp_path, slow_checks):
    engine = make_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    init_db(engine)
    with Session(engine) as session:
        create_user(session, "owner@duebell.test", PASSWORD)
    lockout = Lockout()

    def attempt(index):
        with Session(engine) as session:
            try:
                authenticate(session, "owner@duebell.test", "wrong-password", lockout)
            except AuthError as exc:
                return str(exc)
        return "signed in"

    results = run_together(40, attempt)
    assert len(slow_checks) <= MAX_FAILURES
    assert sum("do not match" in result for result in results) <= MAX_FAILURES
    assert sum("Too many failed attempts" in result for result in results) >= 40 - MAX_FAILURES
    with Session(engine) as session, pytest.raises(AuthError, match="Too many failed attempts"):
        authenticate(session, "owner@duebell.test", PASSWORD, lockout)


def test_parallel_sign_ins_through_the_route_stay_within_the_limit(settings, tmp_path, slow_checks):
    app = create_app(settings=settings, engine=make_engine(f"sqlite:///{tmp_path / 'web.db'}"))
    with Session(app.state.engine) as session:
        create_user(session, "owner@duebell.test", PASSWORD)
    form = {"email": "owner@duebell.test", "password": "wrong-password"}

    def attempt(index):
        response = TestClient(app).post("/login", data=form, follow_redirects=False)
        return response.status_code, "Too many failed attempts" in response.text

    results = run_together(40, attempt)
    assert len(slow_checks) <= MAX_FAILURES
    assert all(status == 401 for status, _ in results)
    assert sum(locked for _, locked in results) >= 40 - MAX_FAILURES
    # The right password, once the burst is over, is refused too. A right
    # password inside the first five would sign in and clear the count.
    right = TestClient(app).post("/login", data=dict(form, password=PASSWORD), follow_redirects=False)
    assert right.status_code == 401 and "Too many failed attempts" in right.text


def test_lockout_drops_expired_entries(monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr(auth.time, "time", lambda: clock[0])
    lockout = Lockout()
    for index in range(50):
        assert lockout.attempt(f"user{index}@duebell.test")
    assert len(lockout) == 50
    clock[0] += LOCKOUT_SECONDS + 1
    assert lockout.attempt("fresh@duebell.test")
    assert len(lockout) == 1


def test_lockout_table_has_a_size_cap_that_keeps_locked_emails():
    lockout = Lockout(max_entries=20)
    for _ in range(MAX_FAILURES):
        assert lockout.attempt("target@duebell.test")
    assert not lockout.attempt("target@duebell.test")
    for index in range(500):
        lockout.attempt(f"random{index}@duebell.test")
    assert len(lockout) <= 20
    assert not lockout.attempt("target@duebell.test")


def test_success_clears_the_count():
    lockout = Lockout()
    for _ in range(MAX_FAILURES - 1):
        assert lockout.attempt("owner@duebell.test")
    lockout.clear("owner@duebell.test")
    for _ in range(MAX_FAILURES):
        assert lockout.attempt("owner@duebell.test")
    assert not lockout.attempt("owner@duebell.test")


@pytest.mark.parametrize("origin", ["https://evil.test", "http://testserver.evil.test", "http://testserver:8001", "null"])
def test_sign_in_refuses_a_post_from_another_site(app, origin):
    with Session(app.state.engine) as session:
        create_user(session, "owner@duebell.test", PASSWORD)
    response = TestClient(app).post(
        "/login",
        data={"email": "owner@duebell.test", "password": PASSWORD},
        headers={"Origin": origin},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert COOKIE_NAME not in response.headers.get("set-cookie", "")


@pytest.mark.parametrize("headers", [{}, {"Origin": "http://testserver"}, {"Origin": "HTTPS://TestServer"}])
def test_sign_in_accepts_its_own_origin_or_none(app, headers):
    with Session(app.state.engine) as session:
        create_user(session, "owner@duebell.test", PASSWORD)
    response = TestClient(app).post(
        "/login",
        data={"email": "owner@duebell.test", "password": PASSWORD},
        headers=headers,
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_the_referrer_policy_lets_a_browser_name_its_own_origin(app):
    """Under no-referrer a browser sends Origin: null on a same-site form post.

    The sign-in check refuses null, so no-referrer locked every browser out
    while these tests, which set Origin by hand, still passed. A policy that
    keeps the origin for same-site requests avoids that.
    """
    policy = TestClient(app).get("/login").headers["referrer-policy"]
    assert policy in {"same-origin", "strict-origin", "strict-origin-when-cross-origin"}


# Sign-in input the lockout must not store, and passwords too long to hash (issue #82).


@pytest.mark.parametrize(
    "email",
    ["a" * 250 + "@x.test", "a" * 1_000_000 + "@x.test", "no-at-sign.test", "   "],
)
def test_sign_in_refuses_an_email_no_user_can_have_without_counting_it(app, email):
    client = TestClient(app)
    for _ in range(3):
        response = client.post("/login", data={"email": email, "password": "wrong-password"})
        assert response.status_code == 401
        assert "do not match" in response.text
    assert len(app.state.lockout) == 0


def test_an_over_long_password_is_refused_without_hashing(app, monkeypatch):
    with Session(app.state.engine) as session:
        create_user(session, "owner@duebell.test", PASSWORD)
    calls = []
    monkeypatch.setattr(auth, "check_password", lambda *args: calls.append(args) or False)
    response = TestClient(app).post(
        "/login", data={"email": "owner@duebell.test", "password": "x" * (auth.MAX_PASSWORD_CHARS + 1)}
    )
    assert response.status_code == 401
    assert "do not match" in response.text
    assert calls == []
    assert len(app.state.lockout) == 1


def test_create_user_refuses_a_password_sign_in_would_refuse(app):
    with Session(app.state.engine) as session, pytest.raises(AuthError):
        create_user(session, "owner@duebell.test", "x" * (auth.MAX_PASSWORD_CHARS + 1))
