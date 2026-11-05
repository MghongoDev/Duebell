"""What every route module shares (issue #30).

The session dependency, the signed-in user's ID, the template set and the
request size limits. The sign-in middleware in app.web sets request.state.user
before any non-public route runs (ADR 0008).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

BASE_DIR = Path(__file__).resolve().parents[1]
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))
MAX_WEBHOOK_BYTES = 1_000_000
MAX_PROMPT_CHARS = 2_000


def get_session(request: Request):
    with Session(request.app.state.engine) as session:
        yield session


def current_user_id(request: Request) -> int:
    """The signed-in user's ID. Only reachable after the sign-in middleware."""
    return request.state.user.id
