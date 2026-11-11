"""Each invoice row offers only what works for it (issue #125)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlmodel import Session

from app.models import Invoice
from tests.test_reminders import seed_invoice
from tests.test_webhooks import create

SCRIPT = Path(__file__).resolve().parent.parent / "app" / "static" / "dashboard.js"

# Runs dashboard.js in node against a stub page, then calls its row logic.
# Every element the script touches is a stub that accepts any call.
HARNESS = r"""
const vm = require("vm");
const fs = require("fs");
const [scriptPath, casesJson] = process.argv.slice(1);
const stub = () => new Proxy(function () {}, {
  get: (target, key) => key === "textContent" ? "[]" : key === Symbol.toPrimitive ? () => "" : stub(),
  apply: () => stub(),
  set: () => true,
});
const context = { document: stub(), window: {}, console };
vm.createContext(context);
const exported = vm.runInContext(
  fs.readFileSync(scriptPath, "utf8") + "\n;({ actionsCell, gridOptions, gridState })",
  context,
);
const cases = JSON.parse(casesJson);
const out = cases.map(({ row, status }) => {
  exported.gridState.status = status || "";
  return {
    html: exported.actionsCell({ data: row, value: row.id }),
    shown: exported.gridOptions.doesExternalFilterPass({ data: row }),
  };
});
process.stdout.write(JSON.stringify(out));
"""


def run_script(cases):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, "-e", HARNESS, str(SCRIPT), json.dumps(cases)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def row(**overrides):
    base = {"id": 7, "status": "SENT", "closed": False, "overdue": False, "risk_level": "LOW", "paypal_invoice_id": None}
    return {**base, **overrides}


def test_a_row_without_a_paypal_id_offers_no_paypal_actions():
    [result] = run_script([{"row": row()}])
    assert 'data-action="pay"' not in result["html"]
    assert 'data-action="refresh"' not in result["html"]
    assert 'data-action="send"' not in result["html"]


def test_an_open_row_with_a_paypal_id_offers_simulate_payment_and_refresh():
    [result] = run_script([{"row": row(paypal_invoice_id="MOCK-1")}])
    assert 'data-action="pay"' in result["html"]
    assert 'data-action="refresh"' in result["html"]


def test_an_unsent_draft_with_a_paypal_id_still_offers_send():
    [result] = run_script([{"row": row(status="DRAFT", paypal_invoice_id="MOCK-1")}])
    assert 'data-action="send"' in result["html"]


def test_a_closed_row_offers_refresh_but_not_simulate_payment():
    [result] = run_script([{"row": row(status="PAID", closed=True, paypal_invoice_id="MOCK-1")}])
    assert 'data-action="refresh"' in result["html"]
    assert 'data-action="pay"' not in result["html"]


def test_the_overdue_filter_keeps_overdue_rows_only():
    results = run_script(
        [
            {"row": row(overdue=True), "status": "OVERDUE"},
            {"row": row(status="OVERDUE"), "status": "OVERDUE"},
            {"row": row(), "status": "OVERDUE"},
        ]
    )
    assert [result["shown"] for result in results] == [True, True, False]


def test_the_status_filter_offers_overdue(client):
    assert '<option value="OVERDUE">' in client.get("/").text


def test_a_row_without_a_paypal_id_reports_none(client):
    invoice_id = seed_invoice(client)
    with Session(client.app.state.engine) as session:
        invoice = session.get(Invoice, invoice_id)
        invoice.paypal_invoice_id = None
        session.add(invoice)
        session.commit()
    [listed] = client.get("/api/invoices").json()
    assert "paypal_invoice_id" in listed
    assert listed["paypal_invoice_id"] is None
    assert '"paypal_invoice_id": null' in client.get("/").text


def test_a_simulated_payment_adds_a_line_to_the_activity_feed(client):
    invoice = create(client)
    assert "Simulated payment" not in client.get("/").text
    assert client.post(f"/api/invoices/{invoice['id']}/simulate-payment").status_code == 200
    page = client.get("/").text
    assert "Simulated payment" in page
    assert invoice["invoice_number"] in page.split("<h2>Activity</h2>", 1)[1]


def test_another_user_does_not_see_the_simulated_payment(client, app):
    from fastapi.testclient import TestClient

    from tests.conftest import sign_in

    invoice = create(client)
    client.post(f"/api/invoices/{invoice['id']}/simulate-payment")
    other = sign_in(TestClient(app), "other@duebell.test")
    assert "Simulated payment" not in other.get("/").text
