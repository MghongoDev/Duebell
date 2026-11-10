"""Risk preview and the risk comparison (issues #22, #25; ADR 0002)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.learning import latest_comparison, refit
from app.routes.common import current_user_id, get_session
from app.services import preview_risk
from app.validation import proposal_to_cents

router = APIRouter()


@router.post("/api/risk/preview", response_class=JSONResponse)
def api_risk_preview(
    request: Request,
    client_email: str = Form(""),
    amount: str = Form(""),
    terms_days: str = Form("14"),
    currency: str = Form("USD"),
    session: Session = Depends(get_session),
):
    """Score a draft form without saving anything, so the form can offer a plan.

    A plain def, so its database reads run off the event loop (issue #48).
    """
    try:
        amount_cents = proposal_to_cents(Decimal(amount.strip()), currency)
        terms = int(terms_days)
    except (InvalidOperation, ValueError):
        return JSONResponse({"ok": False, "error": "Enter an amount and terms first."}, status_code=422)
    result = preview_risk(
        session,
        current_user_id(request),
        email=client_email,
        amount_cents=amount_cents,
        terms_days=terms,
        currency=currency,
    )
    return {"ok": True, "level": result.level, "score": result.score, "reasons": result.reasons}


@router.post("/api/risk/refit", response_class=JSONResponse)
def api_risk_refit(request: Request, session: Session = Depends(get_session)):
    result = refit(session, user_id=current_user_id(request))
    return {"ok": True, "comparison": result.model_dump(mode="json")}


@router.get("/api/risk/comparison", response_class=JSONResponse)
def api_risk_comparison(request: Request, session: Session = Depends(get_session)):
    result = latest_comparison(session, current_user_id(request))
    return {"ok": True, "comparison": result.model_dump(mode="json") if result else None}
