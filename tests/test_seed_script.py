"""The seed script explains a problem instead of crashing (issue #86)."""

from __future__ import annotations

import sys

import pytest
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Client, Invoice, User, make_engine


@pytest.fixture
def run_seed(tmp_path, monkeypatch):
    """Run scripts/seed.py's main against a scratch database; return the engine."""
    from scripts import seed

    url = f"sqlite:///{tmp_path / 'seed.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()

    def run(*flags: str):
        monkeypatch.setattr(sys, "argv", ["seed.py", *flags])
        seed.main()
        return make_engine(url)

    yield run
    get_settings.cache_clear()


def counts(engine) -> tuple[int, int, int]:
    with Session(engine) as session:
        return tuple(len(session.exec(select(model)).all()) for model in (User, Client, Invoice))


def test_short_demo_password_exits_with_one_line_naming_the_minimum(run_seed, monkeypatch):
    monkeypatch.setenv("DEMO_PASSWORD", "short")
    with pytest.raises(SystemExit) as exit_info:
        run_seed("--if-empty", "--require-password")
    message = str(exit_info.value.code)
    assert "DEMO_PASSWORD" in message
    assert "10 characters" in message
    assert "\n" not in message


def test_short_demo_password_leaves_no_data(run_seed, monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_PASSWORD", "short")
    with pytest.raises(SystemExit):
        run_seed()
    assert counts(make_engine(f"sqlite:///{tmp_path / 'seed.db'}")) == (0, 0, 0)


def test_rerun_on_a_seeded_database_changes_nothing(run_seed, monkeypatch, capsys):
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    engine = run_seed()
    assert counts(engine) == (1, 6, 26)
    capsys.readouterr()

    run_seed()

    assert counts(engine) == (1, 6, 26)
    assert "already" in capsys.readouterr().out
