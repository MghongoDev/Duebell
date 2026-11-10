"""Creating, parsing, listing, refreshing and paying invoices.

Issues #9-#11, #14, #23, #25. The model only fills the form (ADR 0004), PayPal
is reached through the gateway (ADR 0001), and schedules wait for approval
(ADR 0007). Every lookup is scoped to the signed-in user (ADR 0008).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError as PydanticValidationError
from sqlmodel import Session

from app.llm import LLMError
from app.models import IntervalUnit
from app.paypal import PayPalError
from app.risk import RiskLevel
from app.routes.common import MAX_PROMPT_CHARS, current_user_id, get_session
from app.routes.dashboard import render_dashboard
from app.schedules import (
    ScheduleBasis,
    create_plan,
    create_recurring,
    draft_scheduled,
    parse_schedule_request,
)
from app.services import (
    create_invoice,
    get_invoice,
    get_or_create_client,
    invoice_rows,
    preview_risk,
    refresh_invoice,
    simulate_payment,
)
from app.validation import FieldError, InvoiceProposal, proposal_to_cents, validate_proposal

router = APIRouter()


@router.post("/invoices")
def create_invoice_form(
    request: Request,
    client_name: str = Form(""),
    client_email: str = Form(""),
    description: str = Form(""),
    amount: str = Form(""),
    currency: str = Form("USD"),
    terms_days: str = Form("14"),
    repeat: str = Form(""),
    repeat_cycles: str = Form(""),
    instalments: str = Form(""),
    instalment_interval: str = Form(IntervalUnit.MONTH.value),
    session: Session = Depends(get_session),
):
    form = {
        "client_name": client_name,
        "client_email": client_email,
        "description": description,
        "amount": amount,
        "currency": currency,
        "terms_days": terms_days,
        "repeat": repeat,
        "repeat_cycles": repeat_cycles,
        "instalments": instalments,
        "instalment_interval": instalment_interval,
    }
    try:
        terms = int(terms_days)
    except ValueError:
        return render_dashboard(
            request,
            session,
            errors=[FieldError("terms_days", "Payment terms must be a whole number.")],
            form=form,
            status_code=422,
        )

    try:
        proposal = InvoiceProposal(
            client_name=client_name,
            client_email=client_email,
            description=description,
            amount=amount or "0",
            currency=currency,
            terms_days=terms,
        )
    except PydanticValidationError as exc:
        errors = [
            FieldError(str(error["loc"][0]) if error["loc"] else "amount", "Enter a valid value.")
            for error in exc.errors()
        ]
        return render_dashboard(request, session, errors=errors, form=form, status_code=422)

    errors = validate_proposal(proposal, request.app.state.settings)
    schedule_request, schedule_errors = parse_schedule_request(
        repeat, repeat_cycles, instalments, instalment_interval
    )
    errors += schedule_errors
    if errors:
        return render_dashboard(request, session, errors=errors, form=form, status_code=422)

    if schedule_request.instalments:
        # The plan only queues the instalments. Each waits for approval.
        # The form offers a split for High risk only, and the server holds that line.
        risk = preview_risk(
            session,
            current_user_id(request),
            email=proposal.client_email,
            amount_cents=proposal_to_cents(proposal.amount, proposal.currency),
            terms_days=proposal.terms_days,
            currency=proposal.currency,
        )
        if risk.level != RiskLevel.HIGH:
            return render_dashboard(
                request,
                session,
                errors=[FieldError("instalments", "Instalments are only for a High-risk invoice.")],
                form=form,
                status_code=422,
            )
        client = get_or_create_client(session, proposal.client_name, proposal.client_email, current_user_id(request))
        basis = ScheduleBasis(
            user_id=current_user_id(request),
            client_id=client.id,
            description=proposal.description.strip(),
            amount_cents=proposal_to_cents(proposal.amount, proposal.currency),
            currency=proposal.currency.strip().upper(),
            terms_days=proposal.terms_days,
        )
        try:
            create_plan(
                session,
                basis,
                instalments=schedule_request.instalments,
                interval_unit=schedule_request.instalment_interval,
            )
        except ValueError as exc:
            return render_dashboard(
                request, session, errors=[FieldError("instalments", str(exc))], form=form, status_code=422
            )
        draft_scheduled(session, scope=current_user_id(request))
        return RedirectResponse(url="/", status_code=303)

    warnings: list[str] = []
    try:
        invoice = create_invoice(
            session,
            proposal,
            request.app.state.settings,
            request.app.state.gateway,
            user_id=current_user_id(request),
            warnings=warnings,
        )
    except PayPalError as exc:
        return render_dashboard(
            request,
            session,
            errors=[FieldError("paypal", str(exc))],
            form=form,
            status_code=502,
        )
    if schedule_request.repeat:
        basis = ScheduleBasis(
            user_id=invoice.user_id,
            client_id=invoice.client_id,
            description=invoice.description,
            amount_cents=invoice.amount_cents,
            currency=invoice.currency,
            terms_days=invoice.terms_days,
        )
        create_recurring(
            session,
            basis,
            interval_unit=schedule_request.repeat,
            cycles=schedule_request.repeat_cycles,
            start=invoice.issued_at,
        )
    return RedirectResponse(url="/?notice=send-unconfirmed" if warnings else "/", status_code=303)


@router.post("/parse", response_class=JSONResponse)
def parse_description(request: Request, description: str = Form("")):
    # A plain def runs in FastAPI's threadpool, so a slow model call does not
    # hold up other requests (issue #48).
    text = description.strip()
    if not text:
        return JSONResponse({"ok": False, "error": "Write a sentence or two first."}, status_code=422)
    if len(text) > MAX_PROMPT_CHARS:
        return JSONResponse(
            {"ok": False, "error": f"Keep the description under {MAX_PROMPT_CHARS:,} characters."},
            status_code=422,
        )
    try:
        proposal = request.app.state.llm(text)
    except LLMError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    # A field the model left out comes back empty and is named in `missing`,
    # so the form can mark it for the person (issue #47).
    fields = proposal.model_dump(mode="json")
    missing = [name for name, value in fields.items() if value is None]
    filled = {name: "" if value is None else value for name, value in fields.items()}
    return JSONResponse({"ok": True, "proposal": filled, "missing": missing})


@router.get("/api/invoices", response_class=JSONResponse)
def api_invoices(request: Request, session: Session = Depends(get_session)):
    return invoice_rows(session, current_user_id(request))


@router.post("/api/invoices/{invoice_id}/refresh", response_class=JSONResponse)
def api_refresh(
    invoice_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    invoice = get_invoice(session, invoice_id, current_user_id(request))
    if invoice is None:
        return JSONResponse({"ok": False, "error": "Invoice not found."}, status_code=404)
    try:
        refresh_invoice(session, invoice, request.app.state.gateway)
    except PayPalError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return {"ok": True, "status": invoice.status}


@router.post("/api/invoices/{invoice_id}/simulate-payment", response_class=JSONResponse)
def api_simulate_payment(
    invoice_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    invoice = get_invoice(session, invoice_id, current_user_id(request))
    if invoice is None:
        return JSONResponse({"ok": False, "error": "Invoice not found."}, status_code=404)
    try:
        simulate_payment(session, invoice, request.app.state.gateway)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    except PayPalError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return {"ok": True, "status": invoice.status}
