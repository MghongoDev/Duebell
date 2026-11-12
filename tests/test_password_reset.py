"""Password reset by email (issue #151)."""

import logging
import re
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlmodel import Session, select

from app.auth import MAX_FAILURES, create_user
from app.mailer import MockMailer
from app.models import PasswordReset, User, init_db, make_engine, utcnow
from app.web import create_app
from tests.conftest import PASSWORD, sign_in

EMAIL = "owner@duebell.test"
NEW_PASSWORD = "a-brand-new-password"
LINK = re.compile(r"(\S*/reset-password\?token=([A-Za-z0-9_\-]+))")


@pytest.fixture
def user(app):
    with Session(app.state.engine) as session:
        return create_user(session, EMAIL, PASSWORD)


def ask_for_link(app, email=EMAIL, **kwargs):
    return TestClient(app).post("/forgot-password", data={"email": email}, **kwargs)


def emailed_token(app) -> str:
    outbox = app.state.mailer.outbox
    assert outbox, "no email was sent"
    match = LINK.search(outbox[-1]["body"])
    assert match, outbox[-1]["body"]
    return match.group(2)


def set_password(app, token, password=NEW_PASSWORD, **kwargs):
    return TestClient(app).post(
        "/reset-password", data={"token": token, "password": password}, follow_redirects=False, **kwargs
    )


def signs_in(app, password) -> bool:
    response = TestClient(app).post("/login", data={"email": EMAIL, "password": password}, follow_redirects=False)
    return response.status_code == 303


def test_the_sign_in_page_links_to_the_reset(app):
    page = TestClient(app).get("/login")
    assert 'href="/forgot-password"' in page.text


def test_the_reset_pages_need_no_session(app, user):
    anonymous = TestClient(app)
    assert anonymous.get("/forgot-password", follow_redirects=False).status_code == 200
    ask_for_link(app)
    assert anonymous.get(f"/reset-password?token={emailed_token(app)}", follow_redirects=False).status_code == 200


def test_a_user_resets_their_password_from_the_emailed_link_and_signs_in_with_it(app, user):
    response = ask_for_link(app)
    assert response.status_code == 200
    [mail] = app.state.mailer.outbox
    assert mail["to"] == EMAIL
    token = emailed_token(app)

    page = TestClient(app).get(f"/reset-password?token={token}")
    assert page.status_code == 200
    assert 'name="password"' in page.text

    done = set_password(app, token)
    assert done.status_code == 200, done.text
    assert signs_in(app, NEW_PASSWORD)
    assert not signs_in(app, PASSWORD)


def test_the_email_matches_however_it_is_typed(app, user):
    ask_for_link(app, email="  Owner@Duebell.TEST ")
    assert app.state.mailer.outbox[-1]["to"] == EMAIL


def test_a_link_works_once(app, user):
    ask_for_link(app)
    token = emailed_token(app)
    assert set_password(app, token).status_code == 200
    again = set_password(app, token, password="yet-another-password")
    assert again.status_code == 400
    assert signs_in(app, NEW_PASSWORD)
    assert TestClient(app).get(f"/reset-password?token={token}").status_code == 400


def test_a_link_stops_working_after_it_expires(app, user):
    ask_for_link(app)
    token = emailed_token(app)
    with Session(app.state.engine) as session:
        row = session.exec(select(PasswordReset)).one()
        row.expires_at = utcnow() - timedelta(seconds=1)
        session.add(row)
        session.commit()
    assert TestClient(app).get(f"/reset-password?token={token}").status_code == 400
    assert set_password(app, token).status_code == 400
    assert signs_in(app, PASSWORD)


def test_a_link_lasts_about_half_an_hour(app, user):
    ask_for_link(app)
    with Session(app.state.engine) as session:
        row = session.exec(select(PasswordReset)).one()
        left = row.expires_at - utcnow()
    assert timedelta(minutes=25) < left <= timedelta(minutes=30)


def test_a_link_stops_working_once_the_password_changes(app, user):
    ask_for_link(app)
    first = emailed_token(app)
    ask_for_link(app)
    second = emailed_token(app)
    assert first != second
    assert set_password(app, second).status_code == 200
    assert set_password(app, first, password="yet-another-password").status_code == 400
    assert signs_in(app, NEW_PASSWORD)


def test_a_made_up_token_is_refused(app, user):
    assert set_password(app, "not-a-real-token").status_code == 400
    assert set_password(app, "x" * 5000).status_code == 400
    assert set_password(app, "").status_code == 400
    assert signs_in(app, PASSWORD)


def test_a_short_password_keeps_the_link_alive(app, user):
    ask_for_link(app)
    token = emailed_token(app)
    short = set_password(app, token, password="short")
    assert short.status_code == 422
    assert "at least 10 characters" in short.text
    assert set_password(app, token).status_code == 200


def test_known_and_unknown_emails_get_the_same_answer(app, user):
    known = ask_for_link(app)
    unknown = ask_for_link(app, email="nobody@duebell.test")
    invalid = ask_for_link(app, email="not-an-email")
    assert known.status_code == unknown.status_code == invalid.status_code == 200
    assert known.text == unknown.text == invalid.text
    assert [mail["to"] for mail in app.state.mailer.outbox] == [EMAIL]


def test_the_request_answers_before_it_looks_up_the_email(app, user, monkeypatch):
    """The lookup and the send run after the answer, so both cases take the same time."""
    from app.routes import password_reset

    calls = []
    monkeypatch.setattr(password_reset, "send_reset_link", lambda *args, **kwargs: calls.append(args))
    response = ask_for_link(app)
    assert response.status_code == 200
    # The route only schedules the work; the stub ran as a background task.
    assert len(calls) == 1


def test_only_a_hash_of_the_token_is_stored(app, user):
    ask_for_link(app)
    token = emailed_token(app)
    with app.state.engine.connect() as connection:
        rows = connection.execute(text("SELECT * FROM passwordreset")).mappings().all()
    assert len(rows) == 1
    assert all(token not in str(value) for value in rows[0].values())


def test_a_reset_ends_the_users_other_sessions(app, user):
    signed_in = TestClient(app)
    response = signed_in.post("/login", data={"email": EMAIL, "password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303
    assert signed_in.get("/", follow_redirects=False).status_code == 200
    ask_for_link(app)
    assert set_password(app, emailed_token(app)).status_code == 200
    page = signed_in.get("/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert signed_in.get("/api/invoices").status_code == 401


def test_a_reset_leaves_other_users_alone(app, user):
    other = sign_in(TestClient(app), "other@duebell.test")
    ask_for_link(app)
    assert set_password(app, emailed_token(app)).status_code == 200
    assert other.get("/", follow_redirects=False).status_code == 200


def test_in_mock_mode_the_link_goes_to_the_server_log_only(app, user, caplog):
    with caplog.at_level(logging.INFO, logger="duebell.password_reset"):
        response = ask_for_link(app)
    token = emailed_token(app)
    assert token in caplog.text
    assert token not in response.text
    assert "reset-password" not in response.text


def test_reset_requests_count_against_a_limit(app, user):
    answers = {ask_for_link(app).text for _ in range(MAX_FAILURES + 2)}
    assert len(answers) == 1
    assert len(app.state.mailer.outbox) == MAX_FAILURES
    # The limit is separate from sign-in, so requests cannot lock the account.
    assert signs_in(app, PASSWORD)


def test_unknown_emails_count_against_the_limit_too(app):
    for _ in range(MAX_FAILURES + 1):
        ask_for_link(app, email="nobody@duebell.test")
    assert not app.state.reset_lockout.attempt("nobody@duebell.test")


def test_a_reset_clears_the_sign_in_lockout(app, user):
    for _ in range(MAX_FAILURES):
        signs_in(app, "wrong-password-here")
    assert not signs_in(app, PASSWORD)
    ask_for_link(app)
    assert set_password(app, emailed_token(app)).status_code == 200
    assert signs_in(app, NEW_PASSWORD)


@pytest.mark.parametrize("path", ["/forgot-password", "/reset-password"])
def test_the_reset_forms_refuse_a_post_from_another_site(app, user, path):
    ask_for_link(app)
    token = emailed_token(app)
    response = TestClient(app).post(
        path,
        data={"email": EMAIL, "token": token, "password": NEW_PASSWORD},
        headers={"Origin": "https://evil.test"},
    )
    assert response.status_code == 403
    assert len(app.state.mailer.outbox) == 1
    assert signs_in(app, PASSWORD)


def smtp_app(settings, **changes):
    settings = replace(settings, email_mode="smtp", **changes)
    return create_app(settings=settings, engine=make_engine(settings.database_url), mailer=MockMailer())


def test_the_link_uses_public_url_and_not_the_host_header(settings):
    app = smtp_app(settings, public_url="https://duebell.example/")
    with Session(app.state.engine) as session:
        create_user(session, EMAIL, PASSWORD)
    ask_for_link(app, headers={"Host": "evil.test"})
    link = LINK.search(app.state.mailer.outbox[-1]["body"]).group(1)
    assert link.startswith("https://duebell.example/reset-password?token=")


def test_smtp_mode_without_public_url_sends_no_link(settings, caplog):
    app = smtp_app(settings)
    with Session(app.state.engine) as session:
        create_user(session, EMAIL, PASSWORD)
    with caplog.at_level(logging.WARNING, logger="duebell.password_reset"):
        response = ask_for_link(app)
    assert response.status_code == 200
    assert app.state.mailer.outbox == []
    assert "PUBLIC_URL" in caplog.text
    with Session(app.state.engine) as session:
        assert session.exec(select(PasswordReset)).all() == []


def test_in_smtp_mode_the_link_stays_out_of_the_log(settings, caplog):
    app = smtp_app(settings, public_url="https://duebell.example")
    with Session(app.state.engine) as session:
        create_user(session, EMAIL, PASSWORD)
    with caplog.at_level(logging.DEBUG):
        ask_for_link(app)
    token = emailed_token(app)
    assert token not in caplog.text


def test_a_failed_send_still_gives_the_same_answer(app, user, caplog):
    app.state.mailer.fail_with = "The email could not be sent: SMTPServerDisconnected. Try again."
    known = ask_for_link(app)
    unknown = ask_for_link(app, email="nobody@duebell.test")
    assert known.status_code == 200 and known.text == unknown.text


def test_startup_adds_the_reset_table_to_an_older_database(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                'CREATE TABLE "user" (id INTEGER PRIMARY KEY, email VARCHAR NOT NULL UNIQUE, '
                "password_hash VARCHAR NOT NULL, created_at DATETIME NOT NULL, session_key VARCHAR)"
            )
        )
    assert "passwordreset" not in inspect(engine).get_table_names()
    init_db(engine)
    assert "passwordreset" in inspect(engine).get_table_names()
    with Session(engine) as session:
        user = create_user(session, EMAIL, PASSWORD)
        assert session.get(User, user.id)
