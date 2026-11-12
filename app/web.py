"""The Duebell app: wiring, the sign-in middleware and security headers.

Issues #9, #11, #24, #30. The routes live in app.routes, one module per
area. Every route except the public paths runs as one signed-in user and
reads through that user's scope (ADR 0008). Sending anything to PayPal or a
client needs an approval route (ADRs 0003, 0007).

Importing this module builds no app and touches no database. The server
serves app.main:app, which calls create_app (issue #120).
"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool

from app.auth import COOKIE_NAME, Lockout, SignupLimit, read_session_cookie, session_key_matches
from app.config import Settings, get_settings
from app.jobs import start_scheduler, stop_scheduler
from app.llm import build_parser
from app.mailer import build_mailer
from app.models import User, init_db, make_engine
from app.paypal import build_gateway
from app.reminders import release_interrupted_reminders
from app.routes import auth, dashboard, invoices, password_reset, reminders, risk, schedules, webhooks
from app.routes.common import BASE_DIR
from app.schedules import release_interrupted

# /logout is public so it clears the cookie and redirects even when the
# session has already expired (issue #84). It only ever signs out.
# /signup answers 404 itself while SIGNUP_ENABLED is off (issue #150).
PUBLIC_PATHS = {"/login", "/logout", "/signup", "/healthz", "/webhooks/paypal"}
# Someone who forgot their password has no session (issue #151).
PUBLIC_PATHS |= {"/forgot-password", "/reset-password"}
# Routes called by scripts, not by a page or form; without a session they
# answer 401 with JSON instead of a redirect (issue #84).
JSON_PATHS = {"/parse"}
ROUTERS = (auth, dashboard, invoices, risk, schedules, reminders, webhooks, password_reset)


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


def _load_user(engine, user_id: int, session_key: str) -> User | None:
    """The user a session cookie names, or None if the cookie's key is not theirs (issue #142)."""
    with Session(engine) as session:
        user = session.get(User, user_id)
        if user is None or not session_key_matches(user, session_key):
            return None
        session.expunge(user)
        return user


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
    with Session(engine) as session:
        release_interrupted(session)
        release_interrupted_reminders(session)
    gateway = gateway or build_gateway(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.scheduler_enabled:
            start_scheduler(app)
        yield
        if app.state.scheduler_enabled:
            stop_scheduler(app)

    # Swagger UI and ReDoc load scripts from a CDN plus an inline script, which
    # the CSP below blocks, so both pages are off. /openapi.json stays and
    # needs sign-in like any other route (issue #124).
    app = FastAPI(title="Duebell", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
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
    # Reset requests count per email like sign-ins, in a table of their own (issue #151).
    app.state.reset_lockout = Lockout()
    app.state.signup_limit = SignupLimit()

    @app.middleware("http")
    async def require_sign_in(request: Request, call_next):
        """Default-deny: every route needs a signed-in user unless listed here."""
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        claim = read_session_cookie(request.app.state.secret_key, request.cookies.get(COOKIE_NAME))
        user = None
        if claim is not None:
            # The database read runs in the threadpool, off the event loop (issue #48).
            user = await run_in_threadpool(_load_user, request.app.state.engine, *claim)
        if user is None:
            # Pages and form posts go to sign-in, so an expired session never
            # shows raw JSON in place of a page (issue #84).
            if not path.startswith("/api/") and path not in JSON_PATHS:
                return RedirectResponse(url="/login", status_code=303)
            return JSONResponse({"ok": False, "error": "Sign in first."}, status_code=401)
        request.state.user = user
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        # Scripts come only from /static, where the vendored AG Grid and htmx
        # live (issue #49). AG Grid and htmx inject <style> tags and style
        # attributes at run time, so styles still allow 'unsafe-inline'.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        return response

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    for module in ROUTERS:
        _add_routes(app, module.router)

    return app

