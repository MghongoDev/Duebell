"""Self-service sign-up behind SIGNUP_ENABLED (issue #150)."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import auth
from app.auth import COOKIE_NAME, AuthError, create_user
from app.models import User, make_engine
from app.web import create_app
from tests.conftest import PASSWORD

EMAIL = "new@duebell.test"


@pytest.fixture
def signup_app(settings):
    on = replace(settings, signup_enabled=True)
    return create_app(settings=on, engine=make_engine(on.database_url))


def users(app) -> list[User]:
    with Session(app.state.engine) as session:
        return list(session.exec(select(User)))


def sign_up(test_client: TestClient, email: str = EMAIL, password: str = PASSWORD, **kwargs):
    return test_client.post("/signup", data={"email": email, "password": password}, follow_redirects=False, **kwargs)


def sign_in(test_client: TestClient, email: str = EMAIL, password: str = PASSWORD):
    return test_client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def test_sign_up_is_off_by_default(settings):
    assert settings.signup_enabled is False


def test_with_sign_up_off_the_page_and_the_post_are_not_found(app):
    anonymous = TestClient(app)
    assert anonymous.get("/signup").status_code == 404
    assert sign_up(anonymous).status_code == 404
    assert users(app) == []
    assert 'href="/signup"' not in anonymous.get("/login").text


def test_with_sign_up_on_the_sign_in_page_links_to_it(signup_app):
    anonymous = TestClient(signup_app)
    assert 'href="/signup"' in anonymous.get("/login").text
    page = anonymous.get("/signup")
    assert page.status_code == 200
    assert 'action="/signup"' in page.text


def test_a_visitor_signs_up_then_signs_in_to_an_empty_dashboard(signup_app):
    visitor = TestClient(signup_app)
    response = sign_up(visitor)
    assert response.status_code == 200
    assert 'action="/login"' in response.text
    [user] = users(signup_app)
    assert user.email == EMAIL
    assert user.session_key  # a new user gets a session key (issue #142)
    assert sign_in(visitor).status_code == 303
    assert visitor.get("/", follow_redirects=False).status_code == 200
    assert visitor.get("/api/invoices").json() == []


def test_sign_up_stores_the_email_as_sign_in_looks_it_up(signup_app):
    visitor = TestClient(signup_app)
    sign_up(visitor, "  New@Duebell.TEST ")
    assert [user.email for user in users(signup_app)] == [EMAIL]
    assert sign_in(visitor).status_code == 303


BAD_INPUT = [
    ("no-at-sign.test", PASSWORD),
    ("a" * 250 + "@x.test", PASSWORD),
    (EMAIL, "short"),
    (EMAIL, "x" * (auth.MAX_PASSWORD_CHARS + 1)),
]


@pytest.mark.parametrize(("email", "password"), BAD_INPUT)
def test_sign_up_refuses_what_the_command_line_tool_refuses(signup_app, email, password):
    with Session(signup_app.state.engine) as session, pytest.raises(AuthError) as cli_error:
        create_user(session, email, password)
    response = sign_up(TestClient(signup_app), email, password)
    assert response.status_code == 400
    assert str(cli_error.value) in response.text
    assert users(signup_app) == []


def test_a_taken_email_gets_the_same_page_as_a_new_one_and_changes_nothing(signup_app):
    with Session(signup_app.state.engine) as session:
        owner = create_user(session, "owner@duebell.test", PASSWORD)
        before = (owner.password_hash, owner.session_key)
    new = sign_up(TestClient(signup_app), "fresh@duebell.test", "another-long-password")
    taken = sign_up(TestClient(signup_app), "owner@duebell.test", "another-long-password")
    assert taken.status_code == new.status_code == 200
    assert taken.text == new.text.replace("fresh@duebell.test", "owner@duebell.test")
    assert COOKIE_NAME not in taken.headers.get("set-cookie", "")
    assert COOKIE_NAME not in new.headers.get("set-cookie", "")
    with Session(signup_app.state.engine) as session:
        stored = session.exec(select(User).where(User.email == "owner@duebell.test")).one()
        assert (stored.password_hash, stored.session_key) == before
    # The owner's password still works, and the one the visitor chose does not.
    assert sign_in(TestClient(signup_app), "owner@duebell.test").status_code == 303
    assert sign_in(TestClient(signup_app), "owner@duebell.test", "another-long-password").status_code == 401


@pytest.mark.parametrize("origin", ["https://evil.test", "http://testserver:8001", "null"])
def test_sign_up_refuses_a_post_from_another_site(signup_app, origin):
    response = sign_up(TestClient(signup_app), headers={"Origin": origin})
    assert response.status_code == 403
    assert users(signup_app) == []


@pytest.mark.parametrize("headers", [{}, {"Origin": "http://testserver"}])
def test_sign_up_accepts_its_own_origin_or_none(signup_app, headers):
    assert sign_up(TestClient(signup_app), headers=headers).status_code == 200
    assert len(users(signup_app)) == 1


def test_sign_up_attempts_are_limited_for_the_whole_process(signup_app, monkeypatch):
    monkeypatch.setattr(signup_app.state, "signup_limit", auth.SignupLimit(max_attempts=2))
    visitor = TestClient(signup_app)
    assert sign_up(visitor, "one@duebell.test").status_code == 200
    assert sign_up(visitor, "two@duebell.test").status_code == 200
    refused = sign_up(visitor, "three@duebell.test")
    assert refused.status_code == 429
    assert len(users(signup_app)) == 2


def test_bad_input_does_not_use_up_the_limit(signup_app, monkeypatch):
    monkeypatch.setattr(signup_app.state, "signup_limit", auth.SignupLimit(max_attempts=1))
    visitor = TestClient(signup_app)
    assert sign_up(visitor, password="short").status_code == 400
    assert sign_up(visitor).status_code == 200


def test_the_limit_frees_up_after_its_window(monkeypatch):
    clock = [1_000.0]
    monkeypatch.setattr(auth.time, "time", lambda: clock[0])
    limit = auth.SignupLimit(max_attempts=1)
    assert limit.attempt()
    assert not limit.attempt()
    clock[0] += auth.SIGNUP_WINDOW_SECONDS
    assert limit.attempt()
