"""Sign-in, sign-out and the health check (issue #24, ADR 0008)."""

from __future__ import annotations

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


@router.post("/login")
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


@router.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response
