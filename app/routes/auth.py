"""Sign-in, sign-out and the health check (issue #24, ADR 0008)."""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session

from app.auth import COOKIE_NAME, SESSION_SECONDS, AuthError, authenticate, make_session_cookie
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
