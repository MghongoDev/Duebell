"""FastAPI app and routes for Duebell (issues #9, #11, #14, #15, #17)."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError as PydanticValidationError
from sqlmodel import Session

from app.config import Settings, get_settings
from app.llm import LLMError, build_parser
from app.models import init_db, make_engine
from app.paypal import PayPalError, build_gateway
from app.services import (
    create_invoice,
    dashboard_totals,
    get_invoice,
    handle_webhook,
    invoice_rows,
    list_events,
    refresh_invoice,
    simulate_payment,
)
from app.validation import FieldError, InvoiceProposal, validate_proposal

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _activity_rows(session: Session) -> list[dict]:
    return [
        {
            "event_id": event.paypal_event_id,
            "event_type": event.event_type,
            "received_at": event.received_at.strftime("%d %b %H:%M"),
        }
        for event in list_events(session)
    ]


def _render_dashboard(
    request: Request,
    session: Session,
    *,
    errors: list[FieldError] | None = None,
    form: dict | None = None,
    status_code: int = 200,
):
    rows = invoice_rows(session)
    return TEMPLATES.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "rows": rows,
            "totals": dashboard_totals(rows),
            "events": _activity_rows(session),
            "errors": errors or [],
            "form": form or {},
        },
        status_code=status_code,
    )


def create_app(
    settings: Settings | None = None,
    engine=None,
    gateway=None,
    llm=None,
) -> FastAPI:
    settings = settings or get_settings()
    engine = engine or make_engine(settings.database_url)
    init_db(engine)
    gateway = gateway or build_gateway(settings)

    app = FastAPI(title="Duebell", version="0.1.0")
    app.state.settings = settings
    app.state.engine = engine
    app.state.gateway = gateway
    app.state.llm = llm or build_parser(settings)

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def get_session(request: Request):
        with Session(request.app.state.engine) as session:
            yield session

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "service": "duebell"}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, session: Session = Depends(get_session)):
        return _render_dashboard(request, session)

    @app.post("/invoices")
    def create_invoice_form(
        request: Request,
        client_name: str = Form(""),
        client_email: str = Form(""),
        description: str = Form(""),
        amount: str = Form(""),
        currency: str = Form("USD"),
        terms_days: str = Form("14"),
        session: Session = Depends(get_session),
    ):
        form = {
            "client_name": client_name,
            "client_email": client_email,
            "description": description,
            "amount": amount,
            "currency": currency,
            "terms_days": terms_days,
        }
        try:
            terms = int(terms_days)
        except ValueError:
            return _render_dashboard(
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
            return _render_dashboard(request, session, errors=errors, form=form, status_code=422)

        errors = validate_proposal(proposal, request.app.state.settings)
        if errors:
            return _render_dashboard(request, session, errors=errors, form=form, status_code=422)

        try:
            create_invoice(session, proposal, request.app.state.settings, request.app.state.gateway)
        except PayPalError as exc:
            return _render_dashboard(
                request,
                session,
                errors=[FieldError("paypal", str(exc))],
                form=form,
                status_code=502,
            )
        return RedirectResponse(url="/", status_code=303)

    @app.post("/parse", response_class=JSONResponse)
    async def parse_description(request: Request):
        form = await request.form()
        text = str(form.get("description", "")).strip()
        if not text:
            return JSONResponse({"ok": False, "error": "Write a sentence or two first."}, status_code=422)
        try:
            proposal = request.app.state.llm(text)
        except LLMError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        return JSONResponse({"ok": True, "proposal": proposal.model_dump(mode="json")})

    @app.get("/api/invoices", response_class=JSONResponse)
    def api_invoices(session: Session = Depends(get_session)):
        return invoice_rows(session)

    @app.post("/api/invoices/{invoice_id}/refresh", response_class=JSONResponse)
    def api_refresh(
        invoice_id: int,
        request: Request,
        session: Session = Depends(get_session),
    ):
        invoice = get_invoice(session, invoice_id)
        if invoice is None:
            return JSONResponse({"ok": False, "error": "Invoice not found."}, status_code=404)
        try:
            refresh_invoice(session, invoice, request.app.state.gateway)
        except PayPalError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        return {"ok": True, "status": invoice.status}

    @app.post("/api/invoices/{invoice_id}/simulate-payment", response_class=JSONResponse)
    def api_simulate_payment(
        invoice_id: int,
        request: Request,
        session: Session = Depends(get_session),
    ):
        invoice = get_invoice(session, invoice_id)
        if invoice is None:
            return JSONResponse({"ok": False, "error": "Invoice not found."}, status_code=404)
        try:
            simulate_payment(session, invoice, request.app.state.gateway)
        except (PayPalError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        return {"ok": True, "status": invoice.status}

    @app.post("/webhooks/paypal", response_class=JSONResponse)
    async def paypal_webhook(request: Request, session: Session = Depends(get_session)):
        body = (await request.body()).decode("utf-8")
        result = handle_webhook(
            session,
            request.app.state.gateway,
            dict(request.headers),
            body,
        )
        status_code = 200 if result.get("ok") else 400
        return JSONResponse(result, status_code=status_code)

    return app


app = create_app()
