"""scripts/create_user.py --claim works for a user who already exists (issue #123)."""

from __future__ import annotations

import getpass
import sys

import pytest
from sqlmodel import Session, select

from app.auth import create_user
from app.config import get_settings
from app.models import Client, Invoice, User, init_db, make_engine
from tests.conftest import PASSWORD


@pytest.fixture
def run_script(tmp_path, monkeypatch):
    """Run scripts/create_user.py's main against a scratch database; return the engine."""
    from scripts import create_user as script

    url = f"sqlite:///{tmp_path / 'users.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("DUEBELL_PASSWORD", raising=False)
    get_settings.cache_clear()
    engine = make_engine(url)
    init_db(engine)

    def run(*args: str):
        monkeypatch.setattr(sys, "argv", ["create_user.py", *args])
        script.main()

    yield engine, run
    get_settings.cache_clear()


def no_prompt(prompt: str = "") -> str:
    raise AssertionError("The script asked for a password.")


def add_ownerless_records(engine) -> None:
    with Session(engine) as session:
        session.add(Client(name="Acme Studio", email="billing@acme.test", user_id=None))
        session.commit()
        client = session.exec(select(Client)).one()
        session.add(Invoice(client_id=client.id, invoice_number="INV-1", amount_cents=120000, user_id=None))
        session.commit()


def test_claim_gives_an_existing_user_the_ownerless_records_without_a_password(run_script, monkeypatch, capsys):
    engine, run = run_script
    with Session(engine) as session:
        user_id = create_user(session, "owner@example.com", PASSWORD).id
    add_ownerless_records(engine)
    monkeypatch.setattr(getpass, "getpass", no_prompt)

    run("  Owner@Example.COM ", "--claim")

    out = capsys.readouterr().out
    assert "Claimed 1 client, 1 invoice." in out
    assert "Created" not in out
    with Session(engine) as session:
        assert {client.user_id for client in session.exec(select(Client))} == {user_id}
        assert {invoice.user_id for invoice in session.exec(select(Invoice))} == {user_id}
        assert len(session.exec(select(User)).all()) == 1


def test_existing_email_without_claim_still_stops_with_the_message(run_script, monkeypatch):
    engine, run = run_script
    with Session(engine) as session:
        create_user(session, "owner@example.com", PASSWORD)
    add_ownerless_records(engine)
    monkeypatch.setenv("DUEBELL_PASSWORD", PASSWORD)

    with pytest.raises(SystemExit) as exit_info:
        run("owner@example.com")

    assert exit_info.value.code == "A user with that email already exists."
    with Session(engine) as session:
        assert {client.user_id for client in session.exec(select(Client))} == {None}


def test_claim_for_a_new_email_creates_the_user_then_claims(run_script, monkeypatch, capsys):
    engine, run = run_script
    add_ownerless_records(engine)
    monkeypatch.setenv("DUEBELL_PASSWORD", PASSWORD)

    run("new@example.com", "--claim")

    out = capsys.readouterr().out
    assert "Created new@example.com." in out
    assert "Claimed 1 client, 1 invoice." in out
    with Session(engine) as session:
        user = session.exec(select(User)).one()
        assert {client.user_id for client in session.exec(select(Client))} == {user.id}
