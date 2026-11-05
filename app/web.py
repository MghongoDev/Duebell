"""FastAPI app and routes for Duebell.

Issues #9, #11, #14, #15, #17, #18, #21-#26. Every route except the public
paths runs as one signed-in user and reads through that user's scope
(ADR 0008). Sending anything to PayPal or a client needs an approval route
(ADRs 0003, 0007).
"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError as PydanticValidationError
from sqlmodel import Session

from app.auth import (
    COOKIE_NAME,
    SESSION_SECONDS,
    AuthError,
    Lockout,
    authenticate,
    make_session_cookie,
    read_session_cookie,
)
from app.config import Settings, get_settings
from app.jobs import start_scheduler, stop_scheduler
from app.learning import latest_comparison, refit
from app.llm import LLMError, build_parser
from app.mailer import MailError, build_mailer
from app.models import Reminder, Schedule, ScheduledInvoice, User, as_utc, init_db, make_engine
from app.ownership import get_owned
from app.paypal import PayPalError, build_gateway
from app.reminders import (
    draft_reminders,
    edit_reminder,
    parse_channels,
    reject_reminder,
    reminder_rows,
    send_reminder,
    sent_channels,
)
from app.schedules import (
    MAX_INSTALMENTS,
    MIN_INSTALMENTS,
    ScheduleBasis,
    add_interval,
    approve_scheduled,
    cancel_schedule,
    create_plan,
    create_recurring,
    draft_scheduled,
    parse_schedule_request,
    reject_scheduled,
    schedule_rows,
)
from app.services import (
    create_invoice,
    dashboard_totals,
    get_invoice,
    get_or_create_client,
    handle_webhook,
    invoice_rows,
    list_events,
    preview_risk,
    refresh_invoice,
    simulate_payment,
)
from app.validation import FieldError, InvoiceProposal, proposal_to_cents, validate_proposal

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
MAX_WEBHOOK_BYTES = 1_000_000
MAX_PROMPT_CHARS = 2_000
PUBLIC_PATHS = {"/login", "/healthz", "/webhooks/paypal"}


def _uid(request: Request) -> int:
    return request.state.user.id


def _activity_rows(session: Session, user_id: int, limit: int = 20) -> list[dict]:
    rows = [
        {
            "event_id": event.paypal_event_id,
            "event_type": event.event_type,
            "at": event.received_at,
        }
        for event in list_events(session, user_id, limit=limit)
    ]
    for reminder in reminder_rows(session, user_id):
        # Reminders sent before channels existed are SENT with no channel list.
        delivered = reminder["sent_channels"] or reminder["status"] == "SENT"
        if not delivered or not reminder["sent_at"]:
            continue
        via = " and ".join(channel.label for channel in reminder["sent_channels"]) or "PayPal"
        rows.append(
            {
                "event_id": reminder["invoice_number"],
                "event_type": f"Reminder sent via {via}",
                "at": datetime.fromisoformat(reminder["sent_at"]),
            }
        )
    rows.sort(key=lambda row: as_utc(row["at"]), reverse=True)
    for row in rows:
        row["received_at"] = row.pop("at").strftime("%d %b %H:%M")
    return rows[:limit]


def _render_dashboard(
    request: Request,
    session: Session,
    *,
    errors: list[FieldError] | None = None,
    form: dict | None = None,
    status_code: int = 200,
):
    user_id = _uid(request)
    rows = invoice_rows(session, user_id)
    return TEMPLATES.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "rows": rows,
            "totals": dashboard_totals(rows),
            "events": _activity_rows(session, user_id),
            "reminders": reminder_rows(session, user_id),
            "user": request.state.user,
            "errors": errors or [],
            "form": form or {},
            "currencies": request.app.state.settings.allowed_currencies,
            "comparison": latest_comparison(session, user_id),
            "schedules": schedule_rows(session, user_id),
            "instalment_range": range(MIN_INSTALMENTS, MAX_INSTALMENTS + 1),
        },
        status_code=status_code,
    )


def create_app(
    settings: Settings | None = None,
    engine=None,
    gateway=None,
    llm=None,
    scheduler_enabled: bool = False,
    mailer=None,
) -> FastAPI:
    settings = settings or get_settings()
    engine = engine or make_engine(settings.database_url)
    init_db(engine)
    gateway = gateway or build_gateway(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.scheduler_enabled:
            start_scheduler(app)
        yield
        if app.state.scheduler_enabled:
            stop_scheduler(app)

    app = FastAPI(title="Duebell", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.gateway = gateway
    app.state.mailer = mailer or build_mailer(settings)
    app.state.llm = llm or build_parser(settings)
    app.state.scheduler_enabled = scheduler_enabled
    app.state.scheduler = None
    # Without SECRET_KEY, sessions last until the process restarts.
    app.state.secret_key = settings.secret_key or secrets.token_urlsafe(32)
    app.state.lockout = Lockout()

    @app.middleware("http")
    async def require_sign_in(request: Request, call_next):
        """Default-deny: every route needs a signed-in user unless listed here."""
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        user_id = read_session_cookie(request.app.state.secret_key, request.cookies.get(COOKIE_NAME))
        user = None
        if user_id is not None:
            with Session(request.app.state.engine) as session:
                user = session.get(User, user_id)
                if user is not None:
                    session.expunge(user)
        if user is None:
            if request.method == "GET" and not path.startswith("/api/"):
                return RedirectResponse(url="/login", status_code=303)
            return JSONResponse({"ok": False, "error": "Sign in first."}, status_code=401)
        request.state.user = user
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        return response

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def get_session(request: Request):
        with Session(request.app.state.engine) as session:
            yield session

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "service": "duebell"}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return TEMPLATES.TemplateResponse(request=request, name="login.html", context={"error": None, "email": ""})

    @app.post("/login")
    def login(
        request: Request,
        email: str = Form(""),
        password: str = Form(""),
        session: Session = Depends(get_session),
    ):
        try:
            user = authenticate(session, email, password, request.app.state.lockout)
        except AuthError as exc:
            return TEMPLATES.TemplateResponse(
                request=request,
                name="login.html",
                context={"error": str(exc), "email": email[:254]},
                status_code=401,
            )
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            COOKIE_NAME,
            make_session_cookie(request.app.state.secret_key, user.id),
            max_age=SESSION_SECONDS,
            httponly=True,
            samesite="lax",
            secure=request.app.state.settings.cookie_secure,
        )
        return response

    @app.post("/logout")
    def logout():
        response = RedirectResponse(url="/login", status_code=303)
        response.delete_cookie(COOKIE_NAME)
        return response

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
        repeat: str = Form(""),
        repeat_cycles: str = Form(""),
        instalments: str = Form(""),
        instalment_interval: str = Form("MONTH"),
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
        schedule_request, schedule_errors = parse_schedule_request(
            repeat, repeat_cycles, instalments, instalment_interval
        )
        errors += schedule_errors
        if errors:
            return _render_dashboard(request, session, errors=errors, form=form, status_code=422)

        if schedule_request.instalments:
            # The plan only queues the instalments. Each waits for approval.
            # The form offers a split for High risk only, and the server holds that line.
            risk = preview_risk(
                session,
                _uid(request),
                email=proposal.client_email,
                amount_cents=proposal_to_cents(proposal.amount),
                terms_days=proposal.terms_days,
                currency=proposal.currency,
            )
            if risk.level != "HIGH":
                return _render_dashboard(
                    request,
                    session,
                    errors=[FieldError("instalments", "Instalments are only for a High-risk invoice.")],
                    form=form,
                    status_code=422,
                )
            client = get_or_create_client(session, proposal.client_name, proposal.client_email, _uid(request))
            basis = ScheduleBasis(
                user_id=_uid(request),
                client_id=client.id,
                description=proposal.description.strip(),
                amount_cents=proposal_to_cents(proposal.amount),
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
                return _render_dashboard(
                    request, session, errors=[FieldError("instalments", str(exc))], form=form, status_code=422
                )
            draft_scheduled(session, scope=_uid(request))
            return RedirectResponse(url="/", status_code=303)

        try:
            invoice = create_invoice(
                session, proposal, request.app.state.settings, request.app.state.gateway, user_id=_uid(request)
            )
        except PayPalError as exc:
            return _render_dashboard(
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
                first_issue=add_interval(invoice.issued_at, schedule_request.repeat, 1),
            )
        return RedirectResponse(url="/", status_code=303)

    @app.post("/api/risk/preview", response_class=JSONResponse)
    async def api_risk_preview(request: Request, session: Session = Depends(get_session)):
        """Score a draft form without saving anything, so the form can offer a plan."""
        form = await request.form()
        try:
            amount_cents = proposal_to_cents(Decimal(str(form.get("amount", "")).strip()))
            terms = int(str(form.get("terms_days", "14")))
        except (InvalidOperation, ValueError):
            return JSONResponse({"ok": False, "error": "Enter an amount and terms first."}, status_code=422)
        result = preview_risk(
            session,
            _uid(request),
            email=str(form.get("client_email", "")),
            amount_cents=amount_cents,
            terms_days=terms,
            currency=str(form.get("currency", "USD")),
        )
        return {"ok": True, "level": result.level, "score": result.score, "reasons": result.reasons}

    @app.post("/api/scheduled/{item_id}/approve", response_class=JSONResponse)
    def api_approve_scheduled(item_id: int, request: Request, session: Session = Depends(get_session)):
        item = get_owned(session, ScheduledInvoice, item_id, _uid(request))
        if item is None:
            return JSONResponse({"ok": False, "error": "Scheduled invoice not found."}, status_code=404)
        try:
            invoice = approve_scheduled(session, item, request.app.state.settings, request.app.state.gateway)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        except PayPalError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        return {"ok": True, "status": item.status, "invoice_id": invoice.id}

    @app.post("/api/scheduled/{item_id}/reject", response_class=JSONResponse)
    def api_reject_scheduled(item_id: int, request: Request, session: Session = Depends(get_session)):
        item = get_owned(session, ScheduledInvoice, item_id, _uid(request))
        if item is None:
            return JSONResponse({"ok": False, "error": "Scheduled invoice not found."}, status_code=404)
        try:
            reject_scheduled(session, item)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        return {"ok": True, "status": item.status}

    @app.post("/api/schedules/{schedule_id}/cancel", response_class=JSONResponse)
    def api_cancel_schedule(schedule_id: int, request: Request, session: Session = Depends(get_session)):
        schedule = get_owned(session, Schedule, schedule_id, _uid(request))
        if schedule is None:
            return JSONResponse({"ok": False, "error": "Schedule not found."}, status_code=404)
        cancel_schedule(session, schedule)
        return {"ok": True, "status": schedule.status}

    @app.post("/parse", response_class=JSONResponse)
    async def parse_description(request: Request):
        form = await request.form()
        text = str(form.get("description", "")).strip()
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
        return JSONResponse({"ok": True, "proposal": proposal.model_dump(mode="json")})

    @app.post("/api/reminders/scan", response_class=JSONResponse)
    def api_scan_reminders(request: Request, session: Session = Depends(get_session)):
        created = draft_reminders(session, request.app.state.settings, scope=_uid(request))
        scheduled = draft_scheduled(session, scope=_uid(request))
        return {"ok": True, "created": len(created), "scheduled": len(scheduled)}

    @app.get("/api/reminders", response_class=JSONResponse)
    def api_reminders(request: Request, session: Session = Depends(get_session)):
        return reminder_rows(session, _uid(request))

    @app.post("/api/reminders/{reminder_id}/approve", response_class=JSONResponse)
    async def api_approve_reminder(
        reminder_id: int,
        request: Request,
        session: Session = Depends(get_session),
    ):
        reminder = get_owned(session, Reminder, reminder_id, _uid(request))
        if reminder is None:
            return JSONResponse({"ok": False, "error": "Reminder not found."}, status_code=404)
        form = await request.form()
        try:
            channels = parse_channels(form.getlist("channels") or form.get("channel"))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        try:
            outcome = send_reminder(
                session,
                reminder,
                request.app.state.gateway,
                request.app.state.mailer,
                channels,
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        except (PayPalError, MailError) as exc:
            session.refresh(reminder)
            return JSONResponse(
                {"ok": False, "error": str(exc), "sent_channels": sent_channels(reminder)},
                status_code=502,
            )
        body = {"ok": True, "status": reminder.status, "sent_channels": sent_channels(reminder)}
        if outcome.warnings:
            body["warning"] = " ".join(outcome.warnings)
        return body

    @app.post("/api/reminders/{reminder_id}/edit", response_class=JSONResponse)
    async def api_edit_reminder(
        reminder_id: int,
        request: Request,
        session: Session = Depends(get_session),
    ):
        reminder = get_owned(session, Reminder, reminder_id, _uid(request))
        if reminder is None:
            return JSONResponse({"ok": False, "error": "Reminder not found."}, status_code=404)
        if reminder.status != "PENDING":
            return JSONResponse(
                {"ok": False, "error": "Only a pending draft can be edited."},
                status_code=409,
            )
        form = await request.form()
        try:
            edit_reminder(session, reminder, str(form.get("draft_text", "")))
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        return {"ok": True, "draft_text": reminder.draft_text}

    @app.post("/api/reminders/{reminder_id}/reject", response_class=JSONResponse)
    def api_reject_reminder(reminder_id: int, request: Request, session: Session = Depends(get_session)):
        reminder = get_owned(session, Reminder, reminder_id, _uid(request))
        if reminder is None:
            return JSONResponse({"ok": False, "error": "Reminder not found."}, status_code=404)
        try:
            reject_reminder(session, reminder)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        return {"ok": True, "status": reminder.status}

    @app.post("/api/risk/refit", response_class=JSONResponse)
    def api_risk_refit(request: Request, session: Session = Depends(get_session)):
        result = refit(session, user_id=_uid(request))
        return {"ok": True, "comparison": result.model_dump(mode="json")}

    @app.get("/api/risk/comparison", response_class=JSONResponse)
    def api_risk_comparison(request: Request, session: Session = Depends(get_session)):
        result = latest_comparison(session, _uid(request))
        return {"ok": True, "comparison": result.model_dump(mode="json") if result else None}

    @app.get("/api/invoices", response_class=JSONResponse)
    def api_invoices(request: Request, session: Session = Depends(get_session)):
        return invoice_rows(session, _uid(request))

    @app.post("/api/invoices/{invoice_id}/refresh", response_class=JSONResponse)
    def api_refresh(
        invoice_id: int,
        request: Request,
        session: Session = Depends(get_session),
    ):
        invoice = get_invoice(session, invoice_id, _uid(request))
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
        invoice = get_invoice(session, invoice_id, _uid(request))
        if invoice is None:
            return JSONResponse({"ok": False, "error": "Invoice not found."}, status_code=404)
        try:
            simulate_payment(session, invoice, request.app.state.gateway)
        except (PayPalError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        return {"ok": True, "status": invoice.status}

    @app.post("/webhooks/paypal", response_class=JSONResponse)
    async def paypal_webhook(request: Request, session: Session = Depends(get_session)):
        try:
            declared = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            declared = 0
        if declared > MAX_WEBHOOK_BYTES:
            return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413)
        raw = await request.body()
        if len(raw) > MAX_WEBHOOK_BYTES:
            return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413)
        body = raw.decode("utf-8")
        result = handle_webhook(
            session,
            request.app.state.gateway,
            dict(request.headers),
            body,
        )
        status_code = 200 if result.get("ok") else 400
        return JSONResponse(result, status_code=status_code)

    return app


app = create_app(scheduler_enabled=True)
