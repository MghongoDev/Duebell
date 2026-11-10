"""Duebell evals for parsing, reminders, and webhook replay (issue #16).

Run the parse eval against a live model with:

    LLM_MODE=live .venv/bin/python scripts/evals.py

The reminder and webhook evals always run offline.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.llm import LLMError, build_parser, live_ready
from app.models import Client, Event, Invoice, init_db, make_engine, utcnow
from app.paypal import MockPayPal
from app.reminders import money, reminder_text
from app.services import create_invoice, handle_webhook
from app.validation import InvoiceProposal

CASES = [
    {
        "text": "Invoice Acme Studio, billing@acme.test, 1200 USD for logo design, net 14 days.",
        "expect": {
            "client_name": "Acme Studio",
            "client_email": "billing@acme.test",
            "amount": "1200.00",
            "currency": "USD",
            "terms_days": 14,
        },
    },
    {
        "text": "Bill Blue Harbor 2400.50 USD for website redesign, payment due in 30 days. Contact pay@blueharbor.test.",
        "expect": {
            "client_name": "Blue Harbor",
            "client_email": "pay@blueharbor.test",
            "amount": "2400.50",
            "currency": "USD",
            "terms_days": 30,
        },
    },
    {
        "text": "Create an invoice for Cedar and Co (hello@cedar.test) for 450.00 USD, copywriting, net 7.",
        "expect": {
            "client_name": "Cedar and Co",
            "client_email": "hello@cedar.test",
            "amount": "450.00",
            "currency": "USD",
            "terms_days": 7,
        },
    },
    {
        "text": "Invoice Redwood Labs, ap@redwood.test, 3100 USD, design system audit, due on receipt.",
        "expect": {
            "client_name": "Redwood Labs",
            "client_email": "ap@redwood.test",
            "amount": "3100.00",
            "currency": "USD",
            "terms_days": 0,
        },
    },
    {
        "text": "Invoice Lumen Works finance@lumen.test 950 USD for product photography, net 21 days.",
        "expect": {
            "client_name": "Lumen Works",
            "client_email": "finance@lumen.test",
            "amount": "950.00",
            "currency": "USD",
            "terms_days": 21,
        },
    },
    {
        "text": "Send Acme Studio billing@acme.test an invoice for 75.25 USD, monthly retainer, net 10.",
        "expect": {
            "client_name": "Acme Studio",
            "client_email": "billing@acme.test",
            "amount": "75.25",
            "currency": "USD",
            "terms_days": 10,
        },
    },
    {
        "text": "Invoice Northwind Studio accounts@northwind.test 1000 USD for brand identity, net 45 days.",
        "expect": {
            "client_name": "Northwind Studio",
            "client_email": "accounts@northwind.test",
            "amount": "1000.00",
            "currency": "USD",
            "terms_days": 45,
        },
    },
    {
        "text": "Invoice Glow Analytics, invoices@glow.test, 9,500.00 USD for a data audit, net 30 days.",
        "expect": {
            "client_name": "Glow Analytics",
            "client_email": "invoices@glow.test",
            "amount": "9500.00",
            "currency": "USD",
            "terms_days": 30,
        },
    },
    {
        "text": "Invoice Pine and Moss, hello@pinemoss.test, 320 USD for packaging design, net 14 days.",
        "expect": {
            "client_name": "Pine and Moss",
            "client_email": "hello@pinemoss.test",
            "amount": "320.00",
            "currency": "USD",
            "terms_days": 14,
        },
    },
    {
        "text": "Invoice Quartz Health, ap@quartz.test, 860.75 USD for a compliance review, net 60 days.",
        "expect": {
            "client_name": "Quartz Health",
            "client_email": "ap@quartz.test",
            "amount": "860.75",
            "currency": "USD",
            "terms_days": 60,
        },
    },
]


def normalise(field: str, value):
    if field == "amount":
        try:
            return Decimal(str(value)).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None
    if field == "terms_days":
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if field == "currency":
        return str(value).strip().upper()
    if field == "client_email":
        return str(value).strip().lower()
    return str(value).strip()


def run_parse_eval(settings: Settings, force_live: bool) -> bool:
    if settings.llm_mode != "live" and not force_live:
        print("Parse eval skipped. Set LLM_MODE=live or pass --live.")
        return True
    if not live_ready(settings):
        # build_parser would quietly fall back to the mock, which only knows
        # the demo sentence, and the score would describe the mock instead.
        print(
            "Parse eval skipped. Live mode needs ANTHROPIC_API_KEY or "
            "ANTHROPIC_AUTH_TOKEN, and ANTHROPIC_BASE_URL."
        )
        return not force_live

    parser = build_parser(replace(settings, llm_mode="live"))
    field_total = 0
    field_correct = 0
    cases_correct = 0

    for case in CASES:
        try:
            proposal = parser(case["text"])
        except LLMError as exc:
            print(f"FAIL parse: {exc} | {case['text'][:60]}")
            field_total += len(case["expect"])
            continue
        wrong = []
        for field, expected in case["expect"].items():
            got = normalise(field, getattr(proposal, field))
            if got != normalise(field, expected):
                wrong.append(f"{field}={got!r} expected {expected!r}")
        field_total += len(case["expect"])
        field_correct += len(case["expect"]) - len(wrong)
        if wrong:
            print(f"FAIL parse: {case['text'][:60]} | " + "; ".join(wrong))
        else:
            cases_correct += 1

    accuracy = field_correct / field_total if field_total else 0.0
    print(
        f"Parse: {cases_correct}/{len(CASES)} cases exact, "
        f"field accuracy {accuracy:.0%} ({field_correct}/{field_total})"
    )
    return accuracy >= 0.75


def run_reminder_eval(settings: Settings) -> bool:
    now = utcnow()
    cases = [
        (-5, "MEDIUM", "friendly"),
        (-10, "LOW", "friendly"),
        (-20, "LOW", "firm"),
        (2, "HIGH", "firm"),
        (0, "MEDIUM", "friendly"),
        (-14, "HIGH", "firm"),
        (-15, "LOW", "firm"),
        (5, "MEDIUM", "friendly"),
        (-1, "HIGH", "firm"),
        (-30, "HIGH", "firm"),
    ]
    passed = 0
    for index, (due_days, risk, expected_tone) in enumerate(cases):
        invoice = Invoice(
            id=index + 1,
            client_id=1,
            invoice_number=f"DUE-EVAL-{index + 1}",
            amount_cents=120000,
            currency="USD",
            due_at=now + timedelta(days=due_days),
            risk_level=risk,
        )
        client = Client(id=1, name="Acme Studio", email="billing@acme.test")
        text, tone = reminder_text(invoice, client, settings, now)
        checks = [
            tone == expected_tone,
            "Acme Studio" in text,
            invoice.invoice_number in text,
            money(invoice.amount_cents) in text,
            invoice.due_at.strftime("%d %b %Y") in text,
        ]
        amounts = re.findall(r"\d[\d,]*\.\d{2}", text)
        checks.append(amounts == [money(invoice.amount_cents)])
        if all(checks):
            passed += 1
        else:
            print(f"FAIL reminder: case {index + 1} checks {checks}")
    print(f"Reminders: {passed}/{len(cases)} cases pass")
    return passed == len(cases)


def run_webhook_eval(settings: Settings) -> bool:
    engine = make_engine("sqlite://")
    init_db(engine)
    settings = replace(settings, mock_webhooks=True)  # the eval posts mock webhooks (issue #38)
    gateway = MockPayPal(accepts_webhooks=settings.mock_webhooks)
    with Session(engine) as session:
        invoice = create_invoice(
            session,
            InvoiceProposal(
                client_name="Acme Studio",
                client_email="billing@acme.test",
                description="Logo design",
                amount=Decimal("1200.00"),
                currency="USD",
                terms_days=14,
            ),
            settings,
            gateway,
            user_id=None,  # an offline eval database has no users
        )
        body = json.dumps(
            {
                "id": "EVAL-EVENT-1",
                "event_type": "INVOICING.INVOICE.PAID",
                "resource": {"id": invoice.paypal_invoice_id},
            }
        )
        first = handle_webhook(session, gateway, {}, body)
        second = handle_webhook(session, gateway, {}, body)
        events = session.exec(select(Event)).all()
        paid = session.get(Invoice, invoice.id).status == "PAID"

        gateway.verify_result = False
        bad = json.dumps(
            {
                "id": "EVAL-EVENT-2",
                "event_type": "INVOICING.INVOICE.PAID",
                "resource": {"id": invoice.paypal_invoice_id},
            }
        )
        rejected = handle_webhook(session, gateway, {}, bad)["ok"] is False

    checks = [
        first["applied"] is True,
        second.get("duplicate") is True,
        len(events) == 1,
        paid,
        rejected,
    ]
    print(f"Webhook: {'pass' if all(checks) else 'FAIL'} checks={checks}")
    return all(checks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Duebell evals.")
    parser.add_argument("--live", action="store_true", help="Run the parse eval even if LLM_MODE is mock.")
    args = parser.parse_args()
    settings = get_settings()

    results = [
        run_parse_eval(settings, args.live),
        run_reminder_eval(settings),
        run_webhook_eval(settings),
    ]
    print("Overall:", "pass" if all(results) else "FAIL")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
