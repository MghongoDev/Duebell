import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.models import make_engine
from app.web import create_app


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
    return TestClient(app)
