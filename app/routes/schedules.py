"""Approving, skipping and cancelling scheduled invoices (issue #25, ADR 0007)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.models import Schedule, ScheduledInvoice
from app.ownership import get_owned
from app.paypal import PayPalError
from app.routes.common import current_user_id, get_session
from app.schedules import approve_scheduled, cancel_schedule, reject_scheduled

router = APIRouter()


@router.post("/api/scheduled/{item_id}/approve", response_class=JSONResponse)
def api_approve_scheduled(item_id: int, request: Request, session: Session = Depends(get_session)):
    item = get_owned(session, ScheduledInvoice, item_id, current_user_id(request))
    if item is None:
        return JSONResponse({"ok": False, "error": "Scheduled invoice not found."}, status_code=404)
    try:
        invoice = approve_scheduled(session, item, request.app.state.settings, request.app.state.gateway)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    except PayPalError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
    return {"ok": True, "status": item.status, "invoice_id": invoice.id}


@router.post("/api/scheduled/{item_id}/reject", response_class=JSONResponse)
def api_reject_scheduled(item_id: int, request: Request, session: Session = Depends(get_session)):
    item = get_owned(session, ScheduledInvoice, item_id, current_user_id(request))
    if item is None:
        return JSONResponse({"ok": False, "error": "Scheduled invoice not found."}, status_code=404)
    try:
        reject_scheduled(session, item)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return {"ok": True, "status": item.status}


@router.post("/api/schedules/{schedule_id}/cancel", response_class=JSONResponse)
def api_cancel_schedule(schedule_id: int, request: Request, session: Session = Depends(get_session)):
    schedule = get_owned(session, Schedule, schedule_id, current_user_id(request))
    if schedule is None:
        return JSONResponse({"ok": False, "error": "Schedule not found."}, status_code=404)
    cancel_schedule(session, schedule)
    return {"ok": True, "status": schedule.status}
