"""The dashboard page and its activity feed (issues #17, #22, #25, #26)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from app.learning import latest_comparison
from app.models import as_utc
from app.reminders import reminder_rows
from app.routes.common import TEMPLATES, current_user_id, get_session
from app.schedules import MAX_INSTALMENTS, MIN_INSTALMENTS, schedule_rows
from app.services import dashboard_totals, invoice_rows, list_events
from app.validation import FieldError

router = APIRouter()


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


def render_dashboard(
    request: Request,
    session: Session,
    *,
    errors: list[FieldError] | None = None,
    form: dict | None = None,
    status_code: int = 200,
):
    user_id = current_user_id(request)
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


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    return render_dashboard(request, session)
