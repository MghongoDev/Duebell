"""A session cookie signs in only the user it was issued to (issue #142)."""

import base64
import json
import time

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session, select

from app import auth
from app.auth import COOKIE_NAME, create_user
from app.models import User, init_db, make_engine
from app.web import create_app
from scripts.seed import empty_database
from tests.conftest import PASSWORD, sign_in


def refused(test_client: TestClient) -> None:
    page = test_client.get("/", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/login"
    api = test_client.get("/api/invoices")
    assert api.status_code == 401
    assert api.json() == {"ok": False, "error": "Sign in first."}


def test_a_cookie_works_for_the_user_it_was_issued_to(client):
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/api/invoices").status_code == 200


def test_a_cookie_for_a_deleted_user_does_not_sign_in_the_next_user_with_that_id(app):
    old = sign_in(TestClient(app), "old@duebell.test")
    empty_database(app.state.engine)
    with Session(app.state.engine) as session:
        new = create_user(session, "new@duebell.test", PASSWORD)
    assert new.id == old.user_id
    refused(old)


def test_a_cookie_made_before_the_upgrade_is_refused(app):
    """An older cookie holds only the user id and expiry, with no session key."""
    signed_in = sign_in(TestClient(app))
    body = json.dumps({"uid": signed_in.user_id, "exp": int(time.time() + auth.SESSION_SECONDS)}).encode()
    encoded = base64.urlsafe_b64encode(body).decode().rstrip("=")
    cookie = f"{encoded}.{auth._sign(app.state.secret_key, body)}"
    refused(TestClient(app, cookies={COOKIE_NAME: cookie}))


def test_a_new_session_key_signs_the_user_out(app):
    signed_in = sign_in(TestClient(app))
    with Session(app.state.engine) as session:
        user = session.get(User, signed_in.user_id)
        user.session_key = auth.new_session_key()
        session.add(user)
        session.commit()
    refused(signed_in)


def test_every_user_gets_a_key(app):
    with Session(app.state.engine) as session:
        first = create_user(session, "a@duebell.test", PASSWORD)
        second = create_user(session, "b@duebell.test", PASSWORD)
        direct = User(email="c@duebell.test", password_hash="x")
        session.add(direct)
        session.commit()
        keys = {first.session_key, second.session_key, direct.session_key}
    assert None not in keys and len(keys) == 3


def test_startup_gives_users_of_an_older_database_a_key(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                'CREATE TABLE "user" (id INTEGER PRIMARY KEY, email VARCHAR NOT NULL UNIQUE, '
                "password_hash VARCHAR NOT NULL, created_at DATETIME NOT NULL)"
            )
        )
        for email in ("a@duebell.test", "b@duebell.test"):
            connection.execute(
                text("INSERT INTO \"user\" (email, password_hash, created_at) VALUES (:e, 'x', '2026-01-01 00:00:00')"),
                {"e": email},
            )
    init_db(engine)
    with Session(engine) as session:
        keys = [user.session_key for user in session.exec(select(User)).all()]
    assert len(keys) == 2 and None not in keys and keys[0] != keys[1]
    assert all(len(key) >= 40 for key in keys)

    # A second startup keeps the keys, so sessions end only once.
    init_db(engine)
    with Session(engine) as session:
        assert [user.session_key for user in session.exec(select(User)).all()] == keys


def test_create_app_fills_in_missing_keys(tmp_path, settings):
    engine = make_engine(f"sqlite:///{tmp_path / 'app.db'}")
    init_db(engine)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO \"user\" (email, password_hash, created_at) VALUES ('a@duebell.test', 'x', '2026-01-01')")
        )
    create_app(settings=settings, engine=engine)
    with Session(engine) as session:
        assert session.exec(select(User)).one().session_key
