"""The seed script explains a problem instead of crashing (issue #86)."""

from __future__ import annotations

import sys
from pathlib import Path

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



def counts_on_disk(tmp_path) -> tuple[int, int, int]:
    """Count through a new engine, since a pooled connection would still read a deleted file."""
    engine = make_engine(f"sqlite:///{tmp_path / 'seed.db'}")
    try:
        return counts(engine)
    finally:
        engine.dispose()


def seed_then_set_password(run_seed, monkeypatch, tmp_path, password: str | None) -> None:
    """Seed with the default password, then set DEMO_PASSWORD for the next run."""
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    run_seed().dispose()
    assert counts_on_disk(tmp_path) == (1, 6, 26)
    if password is not None:
        monkeypatch.setenv("DEMO_PASSWORD", password)


def test_reset_with_a_short_password_keeps_the_data(run_seed, monkeypatch, tmp_path):
    """The password is checked before the database is deleted (issue #102)."""
    seed_then_set_password(run_seed, monkeypatch, tmp_path, "short")

    with pytest.raises(SystemExit) as exit_info:
        run_seed("--reset")

    message = str(exit_info.value.code)
    assert message.startswith("DEMO_PASSWORD is not usable:")
    assert "10 characters" in message
    assert "\n" not in message
    assert counts_on_disk(tmp_path) == (1, 6, 26)


def test_reset_demo_script_with_a_short_password_keeps_the_data(run_seed, monkeypatch, tmp_path):
    """scripts/reset_demo.py keeps the data too (issue #102)."""
    import runpy

    seed_then_set_password(run_seed, monkeypatch, tmp_path, "short")
    monkeypatch.setattr(sys, "argv", ["reset_demo.py"])
    script = Path(__file__).resolve().parents[1] / "scripts" / "reset_demo.py"

    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(script), run_name="__main__")

    assert str(exit_info.value.code).startswith("DEMO_PASSWORD is not usable:")
    assert counts_on_disk(tmp_path) == (1, 6, 26)


def test_reset_requiring_an_unset_password_keeps_the_data(run_seed, monkeypatch, tmp_path):
    seed_then_set_password(run_seed, monkeypatch, tmp_path, None)

    with pytest.raises(SystemExit) as exit_info:
        run_seed("--reset", "--require-password")

    assert "Set DEMO_PASSWORD" in str(exit_info.value.code)
    assert counts_on_disk(tmp_path) == (1, 6, 26)
