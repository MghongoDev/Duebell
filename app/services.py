"""Invoice services for Duebell (issue #9)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Mapping
from uuid import uuid4

from sqlmodel import Session, select

from app.config import Settings
from app.models import Client, Event, Invoice, utcnow
from app.validation import InvoiceProposal, proposal_to_cents

PAID_STATUSES = {"PAID", "MARKED_AS_PAID"}
CLOSED_STATUSES = {"PAID", "MARKED_AS_PAID", "CANCELLED", "REFUNDED"}

EVENT_STATUS = {
    "INVOICING.INVOICE.SENT": "SENT",
    "INVOICING.INVOICE.PAID": "PAID",
    "INVOICING.INVOICE.CANCELLED": "CANCELLED",
    "INVOICING.INVOICE.REFUNDED": "REFUNDED",
}


def get_or_create_client(session: Session, name: str, email: str) -> Client:
    email = email.strip().lower()
    client = session.exec(select(Client).where(Client.email == email)).first()
    if client:
        if name.strip() and client.name != name.strip():
            client.name = name.strip()
            session.add(client)
        return client
    client = Client(name=name.strip(), email=email)
    session.add(client)
    session.commit()
    session.refresh(client)
    return client


def next_invoice_number(session: Session) -> str:
    count = len(session.exec(select(Invoice)).all()) + 1
    return f"DUE-{utcnow():%Y%m%d}-{count:04d}"


def create_invoice(session: Session, proposal: InvoiceProposal, settings: Settings, gateway) -> Invoice:
    client = get_or_create_client(session, proposal.client_name, proposal.client_email)
    now = utcnow()
    invoice = Invoice(
        client_id=client.id,
        invoice_number=next_invoice_number(session),
        description=proposal.description.strip(),
        amount_cents=proposal_to_cents(proposal.amount),
        currency=proposal.currency.strip().upper(),
        terms_days=proposal.terms_days,
        issued_at=now,
        due_at=now + timedelta(days=proposal.terms_days),
        status="DRAFT",
    )
    session.add(invoice)
    session.commit()
    session.refresh(invoice)

    paypal_id = gateway.create_invoice(
        invoice_number=invoice.invoice_number,
        amount_cents=invoice.amount_cents,
        currency=invoice.currency,
        description=invoice.description,
        terms_days=invoice.terms_days,
        client_name=client.name,
        client_email=client.email,
        issued_at=invoice.issued_at,
        due_at=invoice.due_at,
    )
    invoice.paypal_invoice_id = paypal_id
    session.add(invoice)
    session.commit()

    gateway.send_invoice(paypal_id)
    invoice.status = "SENT"
    session.add(invoice)
    session.commit()
    session.refresh(invoice)
    return invoice


def list_invoices(session: Session) -> list[Invoice]:
    return list(session.exec(select(Invoice).order_by(Invoice.created_at.desc())).all())


def get_invoice(session: Session, invoice_id: int) -> Invoice | None:
    return session.get(Invoice, invoice_id)


def client_map(session: Session) -> dict[int, Client]:
    return {client.id: client for client in session.exec(select(Client)).all()}


def invoice_rows(session: Session) -> list[dict]:
    clients = client_map(session)
    now = utcnow()
    rows = []
    for invoice in list_invoices(session):
        client = clients.get(invoice.client_id)
        overdue = invoice.status not in {"PAID", "CANCELLED", "REFUNDED"} and invoice.due_at < now
        rows.append(
            {
                "id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "client_name": client.name if client else "",
                "client_email": client.email if client else "",
                "description": invoice.description,
                "amount_cents": invoice.amount_cents,
                "amount_display": f"{invoice.amount_cents / 100:,.2f}",
                "currency": invoice.currency,
                "terms_days": invoice.terms_days,
                "issued_at": invoice.issued_at.isoformat(timespec="minutes"),
                "due_at": invoice.due_at.isoformat(timespec="minutes"),
                "due_display": invoice.due_at.strftime("%d %b %Y"),
                "paid_at": invoice.paid_at.isoformat(timespec="minutes") if invoice.paid_at else None,
                "status": invoice.status,
                "overdue": overdue,
                "risk_score": invoice.risk_score,
                "risk_level": invoice.risk_level,
                "risk_reasons": [line for line in invoice.risk_reasons.splitlines() if line.strip()],
                "paypal_invoice_id": invoice.paypal_invoice_id,
            }
        )
    return rows


def dashboard_totals(rows: list[dict]) -> dict:
    open_statuses = {"DRAFT", "SENT", "OVERDUE"}
    outstanding = sum(row["amount_cents"] for row in rows if row["status"] in open_statuses)
    overdue = sum(1 for row in rows if row["overdue"])
    paid = sum(row["amount_cents"] for row in rows if row["status"] == "PAID")
    return {
        "outstanding_display": f"{outstanding / 100:,.2f}",
        "overdue_count": overdue,
        "paid_display": f"{paid / 100:,.2f}",
        "invoice_count": len(rows),
    }


def new_invoice_number() -> str:
    return f"DUE-{utcnow():%Y%m%d}-{uuid4().hex[:6].upper()}"


def find_by_paypal_id(session: Session, paypal_invoice_id: str) -> Invoice | None:
    return session.exec(
        select(Invoice).where(Invoice.paypal_invoice_id == paypal_invoice_id)
    ).first()


def apply_status(invoice: Invoice, status: str) -> None:
    invoice.status = status
    if status in PAID_STATUSES and invoice.paid_at is None:
        invoice.paid_at = utcnow()


def refresh_invoice(session: Session, invoice: Invoice, gateway) -> Invoice:
    if not invoice.paypal_invoice_id:
        return invoice
    status = gateway.get_invoice(invoice.paypal_invoice_id)
    apply_status(invoice, status)
    session.add(invoice)
    session.commit()
    session.refresh(invoice)
    return invoice


def simulate_payment(session: Session, invoice: Invoice, gateway) -> Invoice:
    if not invoice.paypal_invoice_id:
        raise ValueError("Invoice has no PayPal id yet.")
    gateway.record_payment(invoice.paypal_invoice_id, invoice.amount_cents, invoice.currency)
    apply_status(invoice, "PAID")
    session.add(invoice)
    session.commit()
    session.refresh(invoice)
    return invoice


def handle_webhook(session: Session, gateway, headers: Mapping[str, str], body: str) -> dict:
    if not gateway.verify_webhook(headers, body):
        return {"ok": False, "error": "invalid signature"}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid json"}

    event_id = payload.get("id") or ""
    event_type = payload.get("event_type") or ""
    if not event_id:
        return {"ok": False, "error": "missing event id"}

    existing = session.get(Event, event_id)
    if existing:
        return {"ok": True, "duplicate": True}

    resource = payload.get("resource") or {}
    paypal_invoice_id = resource.get("id") or (resource.get("invoice") or {}).get("id") or ""
    status = EVENT_STATUS.get(event_type)
    applied = False
    if paypal_invoice_id and status:
        invoice = find_by_paypal_id(session, paypal_invoice_id)
        if invoice:
            apply_status(invoice, status)
            session.add(invoice)
            applied = True

    session.add(
        Event(
            paypal_event_id=event_id,
            event_type=event_type,
            payload=body[:20000],
        )
    )
    session.commit()
    return {"ok": True, "applied": applied, "status": status}


def list_events(session: Session, limit: int = 20) -> list[Event]:
    return list(
        session.exec(select(Event).order_by(Event.received_at.desc()).limit(limit)).all()
    )
