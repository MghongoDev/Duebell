"""A draft is never overdue, and Skip or Cancel drops a draft PayPal never took (issue #116)."""

from datetime import timedelta

from sqlmodel import Session

from app.models import Invoice, ScheduledInvoice, utcnow
from app.services import dashboard_totals, invoice_rows
from tests.test_link_before_paypal import Crash, _approve_until_crash, _first_item
from tests.test_reminders import seed_invoice
from tests.test_schedules import items


def _set(client, model, row_id, **values):
    with Session(client.app.state.engine) as session:
        row = session.get(model, row_id)
        for key, value in values.items():
            setattr(row, key, value)
        session.add(row)
        session.commit()


def _invoice(client, invoice_id):
    with Session(client.app.state.engine) as session:
        return session.get(Invoice, invoice_id)


def test_a_draft_past_its_due_time_is_not_overdue(client):
    # PayPal holds it unsent after a failed send, on terms of 0 days.
    invoice_id = seed_invoice(client)
    _set(client, Invoice, invoice_id, status="DRAFT", terms_days=0, due_at=utcnow() - timedelta(seconds=1))

    with Session(client.app.state.engine) as session:
        rows = invoice_rows(session, client.user_id)
    totals = dashboard_totals(rows)

    [row] = rows
    assert row["overdue"] is False
    assert totals["overdue_count"] == 0
    assert totals["overdue_by_currency"] == {}
    assert totals["outstanding"] == {}


def test_a_sent_invoice_past_its_due_time_is_still_overdue(client):
    seed_invoice(client)
    with Session(client.app.state.engine) as session:
        rows = invoice_rows(session, client.user_id)
    assert rows[0]["overdue"] is True
    assert dashboard_totals(rows)["overdue_count"] == 1


def _item_with_unpublished_draft(client):
    """A pending item linked to a draft a restart left before PayPal created it (issue #74)."""
    item = _first_item(client)
    gateway = client.app.state.gateway
    real_create = gateway.create_invoice

    def stop(**facts):
        raise Crash()

    gateway.create_invoice = stop
    _approve_until_crash(client, item.id)
    gateway.create_invoice = real_create
    # Startup puts the item a restart left SENDING back to PENDING.
    _set(client, ScheduledInvoice, item.id, status="PENDING")
    item = items(client)[0]
    draft = _invoice(client, item.invoice_id)
    assert draft.status == "DRAFT" and not draft.paypal_invoice_id
    return item, draft


def test_skip_deletes_a_linked_draft_paypal_never_took(client):
    item, draft = _item_with_unpublished_draft(client)

    assert client.post(f"/api/scheduled/{item.id}/reject").json() == {"ok": True, "status": "REJECTED"}

    after = items(client)[0]
    assert after.status == "REJECTED" and after.invoice_id is None
    assert _invoice(client, draft.id) is None
    assert client.app.state.gateway.invoices == {}


def test_cancel_deletes_a_pending_items_draft_paypal_never_took(client):
    item, draft = _item_with_unpublished_draft(client)

    client.post(f"/api/schedules/{item.schedule_id}/cancel")

    after = items(client)[0]
    assert after.status == "CANCELLED" and after.invoice_id is None
    assert _invoice(client, draft.id) is None


def test_cancel_leaves_the_draft_of_an_item_an_approval_holds(client):
    # The approval may be creating it in PayPal now; it drops the draft itself if PayPal refuses.
    item, draft = _item_with_unpublished_draft(client)
    _set(client, ScheduledInvoice, item.id, status="SENDING")

    client.post(f"/api/schedules/{item.schedule_id}/cancel")

    after = items(client)[0]
    assert after.status == "CANCELLED" and after.invoice_id == draft.id
    assert _invoice(client, draft.id) is not None
