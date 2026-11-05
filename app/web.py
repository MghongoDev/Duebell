"""The Duebell app: wiring, the sign-in middleware and security headers.

Issues #9, #11, #24, #30. The routes live in app.routes, one module per
area. Every route except the public paths runs as one signed-in user and
reads through that user's scope (ADR 0008). Sending anything to PayPal or a
client needs an approval route (ADRs 0003, 0007).
"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from app.auth import COOKIE_NAME, Lockout, read_session_cookie
from app.config import Settings, get_settings
from app.jobs import start_scheduler, stop_scheduler
from app.llm import build_parser
from app.mailer import build_mailer
from app.models import User, init_db, make_engine
from app.paypal import build_gateway
from app.routes import auth, dashboard, invoices, reminders, risk, schedules, webhooks
from app.routes.common import BASE_DIR

PUBLIC_PATHS = {"/login", "/healthz", "/webhooks/paypal"}
ROUTERS = (auth, dashboard, invoices, risk, schedules, reminders, webhooks)


def _add_routes(app: FastAPI, router: APIRouter) -> None:
    """Register a module's routes on the app itself.

    FastAPI 0.162's include_router keeps included routes behind a lazy
    wrapper, so app.routes would no longer list them. The test that checks
    every private route turns away anonymous requests walks app.routes, so
    the routes go on the app directly, through the public add_api_route.
    """
    for route in router.routes:
        unsupported = [
            option
            for option, value in (
                ("dependencies", route.dependencies),
                ("status_code", route.status_code),
                ("response_model", route.response_model),
            )
            if value
        ]
        if unsupported:
            # Fail at start rather than drop an option no route uses yet.
            raise RuntimeError(f"{route.path}: _add_routes does not copy {', '.join(unsupported)}.")
        app.add_api_route(
            route.path,
            route.endpoint,
            methods=sorted(route.methods),
            response_class=route.response_class,
            name=route.name,
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

    for module in ROUTERS:
        _add_routes(app, module.router)

    return app


app = create_app(scheduler_enabled=True)
