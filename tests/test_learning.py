"""Refit the risk comparison on real history (issue #22)."""

import random
from datetime import timedelta

from sqlmodel import Session

from app.learning import MIN_ROWS, latest_comparison, outcome, refit, training_rows
from app.models import Client, Invoice, User, init_db, make_engine, utcnow
from app.ownership import EVERY_USER
from scripts.seed import seed


def engine():
    db = make_engine("sqlite://")
    init_db(db)
    return db


def add_history(session, clients=12, per_client=6, seed_value=7) -> int:
    """Clients with fixed habits: some always on time, some usually late. Returns the owner's id."""
    rng = random.Random(seed_value)
    now = utcnow()
    owner = User(email="owner@duebell.test", password_hash="x")
    session.add(owner)
    session.commit()
    for index in range(clients):
        client = Client(name=f"Client {index}", email=f"c{index}@example.test", user_id=owner.id)
        session.add(client)
        session.commit()
        session.refresh(client)
        habitually_late = index % 2 == 0
        for step in range(per_client):
            issued = now - timedelta(days=400 - step * 45)
            due = issued + timedelta(days=30)
            late = rng.random() < (0.85 if habitually_late else 0.1)
            paid = due + timedelta(days=rng.randint(3, 20) if late else -rng.randint(0, 5))
            session.add(
                Invoice(
                    user_id=owner.id,
                    client_id=client.id,
                    invoice_number=f"H-{index}-{step}",
                    amount_cents=100_000,
                    terms_days=30,
                    issued_at=issued,
                    due_at=due,
                    paid_at=paid,
                    status="PAID",
                )
            )
    session.commit()
    return owner.id


def test_demo_seed_is_below_the_minimum_and_says_so():
    db = engine()
    with Session(db) as session:
        seed(session)
        demo_id = session.exec(__import__("sqlmodel").select(User.id)).one()
        result = refit(session, user_id=demo_id)
        assert result.status == "NOT_ENOUGH_HISTORY"
        assert result.logistic_auc is None
        assert f"needs {MIN_ROWS}" in result.message
        assert latest_comparison(session, demo_id).id == result.id


def test_refit_on_enough_history_reports_both_models():
    db = engine()
    with Session(db) as session:
        owner_id = add_history(session)
        result = refit(session, user_id=owner_id)
    assert result.status == "OK"
    assert result.rows_used == 72
    assert result.test_rows == 18
    for value in (result.baseline_accuracy, result.baseline_auc, result.logistic_accuracy, result.logistic_auc):
        assert 0.0 <= value <= 1.0
    assert result.logistic_auc > 0.6


def test_outcome_only_counts_known_results():
    now = utcnow()
    paid_late = Invoice(client_id=1, invoice_number="A", amount_cents=1, status="PAID",
                        due_at=now - timedelta(days=10), paid_at=now - timedelta(days=2))
    paid_on_time = Invoice(client_id=1, invoice_number="B", amount_cents=1, status="PAID",
                           due_at=now - timedelta(days=10), paid_at=now - timedelta(days=12))
    overdue = Invoice(client_id=1, invoice_number="C", amount_cents=1, status="SENT",
                      due_at=now - timedelta(days=1))
    not_due = Invoice(client_id=1, invoice_number="D", amount_cents=1, status="SENT",
                      due_at=now + timedelta(days=5))
    draft = Invoice(client_id=1, invoice_number="E", amount_cents=1, status="DRAFT",
                    due_at=now - timedelta(days=5))
    assert [outcome(item, now) for item in (paid_late, paid_on_time, overdue, not_due, draft)] == [1, 0, 1, None, None]


def test_features_never_see_a_later_payment():
    db = engine()
    now = utcnow()
    with Session(db) as session:
        client = Client(name="Acme", email="a@example.test")
        session.add(client)
        session.commit()
        first_issued = now - timedelta(days=100)
        # The first invoice was paid very late, after the second was issued.
        session.add(Invoice(client_id=client.id, invoice_number="1", amount_cents=100, status="PAID",
                            issued_at=first_issued, due_at=first_issued + timedelta(days=10),
                            paid_at=first_issued + timedelta(days=80)))
        session.add(Invoice(client_id=client.id, invoice_number="2", amount_cents=100, status="PAID",
                            issued_at=first_issued + timedelta(days=20), due_at=first_issued + timedelta(days=30),
                            paid_at=first_issued + timedelta(days=30)))
        session.commit()
        rows = training_rows(session, now, scope=EVERY_USER)
    second = rows[1]
    # As of the second invoice's issue date the first was 10 days overdue and unpaid,
    # not the 70 days late it ended up.
    assert second["average_days_late"] == 10.0
    assert second["new_client"] == 0.0


def test_http_refit_and_dashboard(client):
    response = client.post("/api/risk/refit")
    assert response.json()["comparison"]["status"] == "NOT_ENOUGH_HISTORY"
    page = client.get("/").text
    assert "Not enough history" in page
    assert client.get("/api/risk/comparison").json()["comparison"]["rows_used"] == 0


def test_marked_as_paid_counts_as_a_known_outcome():
    now = utcnow()
    marked = Invoice(client_id=1, invoice_number="M", amount_cents=1, status="MARKED_AS_PAID",
                     due_at=now - timedelta(days=10), paid_at=now - timedelta(days=12))
    assert outcome(marked, now) == 0


def test_scope_is_required_and_a_refit_never_spans_users():
    import pytest

    db = engine()
    with Session(db) as session:
        with pytest.raises(TypeError):
            training_rows(session, scope=None)
        with pytest.raises(TypeError):
            refit(session, user_id=EVERY_USER)
