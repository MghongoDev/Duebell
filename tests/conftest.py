import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import auth
from app.auth import create_user
from app.config import Settings
from app.models import make_engine
from app.web import create_app

PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch):
    """Production scrypt cost makes each sign-in slow. Hashes record their cost."""
    monkeypatch.setitem(auth.SCRYPT, "n", 2**8)


def sign_in(test_client: TestClient, email: str = "owner@duebell.test") -> TestClient:
    """Create a user in the app's database and sign the client in as them.

    The user's id is kept on the client as `user_id`, so tests that write rows
    directly can give them the same owner the HTTP routes filter by.
    """
    with Session(test_client.app.state.engine) as session:
        user = create_user(session, email, PASSWORD)
        test_client.user_id = user.id
    response = test_client.post("/login", data={"email": email, "password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return test_client


@pytest.fixture
def settings():
    return Settings(
        database_url="sqlite://",
        amount_cap_cents=1_000_000,
        allowed_currencies=("USD",),
        terms_min_days=0,
        terms_max_days=120,
    )


@pytest.fixture
def app(settings):
    engine = make_engine(settings.database_url)
    return create_app(settings=settings, engine=engine)


@pytest.fixture
def client(app):
    return sign_in(TestClient(app))
