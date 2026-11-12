"""Sign-in, sign-out, sign-up and the health check (issues #24, #150; ADR 0008)."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from app.auth import (
    COOKIE_NAME,
    SESSION_SECONDS,
    AuthError,
    TooManySignups,
    authenticate,
    make_session_cookie,
    sign_up,
)
from app.routes.common import TEMPLATES, get_session

router = APIRouter()


@router.get("/healthz")
def healthz():
    return {"ok": True, "service": "duebell"}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return TEMPLATES.TemplateResponse(request=request, name="login.html", context={"error": None, "email": ""})


def from_another_site(request: Request) -> bool:
    """True if the Origin header names a host other than this request's own (issue #45).

    A missing Origin passes, since some clients omit it. "null" and anything
    unparsable count as another site. Only the host and port are compared, so
    an HTTPS proxy in front of the app does not matter.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return False
    host = request.headers.get("host", "").lower()
    try:
        origin_host = urlsplit(origin).netloc.lower()
    except ValueError:
        return True
    return not host or origin_host != host


@router.post("/login")
def login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    session: Session = Depends(get_session),
):
    if from_another_site(request):
        return TEMPLATES.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Sign in from the Duebell sign-in page.", "email": ""},
            status_code=403,
        )
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
        make_session_cookie(request.app.state.secret_key, user.id, user.session_key),
        max_age=SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.app.state.settings.cookie_secure,
    )
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


# Shown after every sign-up that passes the checks, whether or not the email
# was free, so the page never reveals that an email is registered (issue #150).
SIGNED_UP = "Now sign in with the email and password you chose. If that email already had an account, use its own password."


def _require_signup(request: Request) -> None:
    """Answer 404, as for a route that does not exist, while SIGNUP_ENABLED is off (issue #150)."""
    if not request.app.state.settings.signup_enabled:
        raise HTTPException(status_code=404)


def _signup_page(request: Request, error: str, email: str, status_code: int):
    return TEMPLATES.TemplateResponse(
        request=request,
        name="signup.html",
        context={"error": error, "email": email[:254]},
        status_code=status_code,
    )


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    _require_signup(request)
    return _signup_page(request, None, "", 200)


@router.post("/signup")
def signup(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    session: Session = Depends(get_session),
):
    """Create an empty account, then show the sign-in page (issue #150).

    A new email and a taken one get the same page and no session cookie, so
    the answer does not say whether the email has an account. The visitor
    signs in as the next step.
    """
    _require_signup(request)
    if from_another_site(request):
        return _signup_page(request, "Sign up from the Duebell sign-up page.", "", 403)
    try:
        sign_up(session, email, password, request.app.state.signup_limit)
    except TooManySignups as exc:
        return _signup_page(request, str(exc), email, 429)
    except AuthError as exc:
        return _signup_page(request, str(exc), email, 400)
    return TEMPLATES.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": None, "notice": SIGNED_UP, "email": email.strip()[:254]},
    )
