"""Scanning, editing, tone switching, approving and rejecting reminders (issues #15, #26, #48, #152; ADR 0003)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.mailer import MailError
from app.models import Reminder, ReminderStatus
from app.ownership import get_owned
from app.paypal import PayPalError
from app.reminders import (
    TONES,
    EditedDraft,
    draft_reminders,
    edit_reminder,
    parse_channels,
    reject_reminder,
    reminder_rows,
    send_reminder,
    sent_channels,
    switch_tone,
)
from app.routes.common import current_user_id, get_session
from app.schedules import draft_scheduled

router = APIRouter()


@router.post("/api/reminders/scan", response_class=JSONResponse)
def api_scan_reminders(request: Request, session: Session = Depends(get_session)):
    created = draft_reminders(session, request.app.state.settings, scope=current_user_id(request))
    scheduled = draft_scheduled(session, scope=current_user_id(request))
    return {"ok": True, "created": len(created), "scheduled": len(scheduled)}


@router.get("/api/reminders", response_class=JSONResponse)
def api_reminders(request: Request, session: Session = Depends(get_session)):
    return reminder_rows(session, current_user_id(request))


@router.post("/api/reminders/{reminder_id}/approve", response_class=JSONResponse)
def api_approve_reminder(
    reminder_id: int,
    request: Request,
    channels: list[str] = Form([]),
    channel: str = Form(""),
    session: Session = Depends(get_session),
):
    # A plain def runs in FastAPI's threadpool, so PayPal and the mailer do not
    # hold up other requests. send_reminder claims the draft first, so two
    # approvals at once send one reminder (issue #48).
    reminder = get_owned(session, Reminder, reminder_id, current_user_id(request))
    if reminder is None:
        return JSONResponse({"ok": False, "error": "Reminder not found."}, status_code=404)
    try:
        channels = parse_channels(channels or channel)
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


@router.post("/api/reminders/{reminder_id}/edit", response_class=JSONResponse)
def api_edit_reminder(
    reminder_id: int,
    request: Request,
    draft_text: str = Form(""),
    session: Session = Depends(get_session),
):
    reminder = get_owned(session, Reminder, reminder_id, current_user_id(request))
    if reminder is None:
        return JSONResponse({"ok": False, "error": "Reminder not found."}, status_code=404)
    if reminder.status != ReminderStatus.PENDING:
        return JSONResponse(
            {"ok": False, "error": "Only a pending draft can be edited."},
            status_code=409,
        )
    try:
        edit_reminder(session, reminder, draft_text)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    return {"ok": True, "draft_text": reminder.draft_text}


@router.post("/api/reminders/{reminder_id}/tone", response_class=JSONResponse)
def api_switch_tone(
    reminder_id: int,
    request: Request,
    tone: str = Form(""),
    replace_edit: bool = Form(False),
    session: Session = Depends(get_session),
):
    """Rebuild a pending draft as friendly or firm (issue #152).

    A hand-edited draft answers 409 with "confirm": true until the user agrees
    to lose their edits and posts again with replace_edit.
    """
    reminder = get_owned(session, Reminder, reminder_id, current_user_id(request))
    if reminder is None:
        return JSONResponse({"ok": False, "error": "Reminder not found."}, status_code=404)
    if reminder.status != ReminderStatus.PENDING:
        return JSONResponse({"ok": False, "error": "Only a pending draft can switch tone."}, status_code=409)
    try:
        switch_tone(session, reminder, tone, request.app.state.settings, replace_edit=replace_edit)
    except EditedDraft as exc:
        return JSONResponse({"ok": False, "error": str(exc), "confirm": True}, status_code=409)
    except ValueError as exc:
        status = 422 if tone not in TONES else 409
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=status)
    return {"ok": True, "tone": reminder.tone, "draft_text": reminder.draft_text}


@router.post("/api/reminders/{reminder_id}/reject", response_class=JSONResponse)
def api_reject_reminder(reminder_id: int, request: Request, session: Session = Depends(get_session)):
    reminder = get_owned(session, Reminder, reminder_id, current_user_id(request))
    if reminder is None:
        return JSONResponse({"ok": False, "error": "Reminder not found."}, status_code=404)
    try:
        reject_reminder(session, reminder)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return {"ok": True, "status": reminder.status}
