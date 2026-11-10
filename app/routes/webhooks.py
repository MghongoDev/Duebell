"""The PayPal webhook: the only public write route (issue #11, ADR 0005)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from app.routes.common import MAX_WEBHOOK_BYTES, get_session
from app.services import handle_webhook

router = APIRouter()


@router.post("/webhooks/paypal", response_class=JSONResponse)
async def paypal_webhook(request: Request, session: Session = Depends(get_session)):
    try:
        declared = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_WEBHOOK_BYTES:
        return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413)
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BYTES:
        return JSONResponse({"ok": False, "error": "payload too large"}, status_code=413)
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError:
        # A body that is not UTF-8 cannot be valid JSON (issue #37).
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    result = handle_webhook(
        session,
        request.app.state.gateway,
        dict(request.headers),
        body,
    )
    status_code = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status_code)
