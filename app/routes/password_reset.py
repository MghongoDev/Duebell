"""Forgot password and reset password pages (issue #151, ADR 0008).

Both paths are public. Their posts pass the same Origin check as sign-in.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from app.auth import AuthError, normalize_email, valid_email
from app.password_reset import (
    BAD_LINK,
    RESET_SECONDS,
    BadResetLink,
    find_reset,
    reset_link_base,
    reset_password,
    send_reset_link,
)
from app.routes.auth import from_another_site
from app.routes.common import TEMPLATES, get_session

router = APIRouter()

PAGE = "password_reset.html"
SENT = (
    "If an account uses that email, we have sent it a link to choose a new password. "
    f"The link works once, within {RESET_SECONDS // 60} minutes."
)


def _page(request: Request, step: str, status_code: int = 200, **context):
    context = {"step": step, "error": None, "message": None, "token": "", **context}
    return TEMPLATES.TemplateResponse(request=request, name=PAGE, context=context, status_code=status_code)


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    return _page(request, "ask")


@router.post("/forgot-password")
def forgot_password(request: Request, background: BackgroundTasks, email: str = Form("")):
    """Every email gets the same answer before anything looks it up.

    The request is counted per email, like a sign-in. One that is over the
    limit, or not an email any user could have, sends nothing. The lookup
    and the send run after the answer goes out, so the time taken says
    nothing about the account either.
    """
    if from_another_site(request):
        return _page(request, "ask", 403, error="Ask from the Duebell reset page.")
    email = normalize_email(email)
    if valid_email(email) and request.app.state.reset_lockout.attempt(email):
        state = request.app.state
        base_url = reset_link_base(state.settings, str(request.base_url))
        background.add_task(send_reset_link, state.engine, state.mailer, state.settings, email, base_url)
    return _page(request, "sent", message=SENT)


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str = "", session: Session = Depends(get_session)):
    if find_reset(session, token) is None:
        return _page(request, "bad_link", 400, error=BAD_LINK)
    return _page(request, "choose", token=token)


@router.post("/reset-password")
def reset_password_submit(
    request: Request,
    token: str = Form(""),
    password: str = Form(""),
    session: Session = Depends(get_session),
):
    if from_another_site(request):
        return _page(request, "bad_link", 403, error="Choose a new password from the Duebell reset page.")
    try:
        user = reset_password(session, token, password)
    except BadResetLink:
        return _page(request, "bad_link", 400, error=BAD_LINK)
    except AuthError as exc:
        return _page(request, "choose", 422, error=str(exc), token=token)
    # The user proved they read the email, so a sign-in lockout on it ends too.
    request.app.state.lockout.clear(user.email)
    return _page(request, "done", message="Your password is changed. Sign in with the new one.")
