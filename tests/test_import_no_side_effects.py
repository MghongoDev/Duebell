"""Importing the web module builds no app and touches no database (issue #120).

The checks import app.web. A running server may be mid-send on a reminder or a
scheduled invoice; an import must not free its claim, or a second click sends
again.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlmodel import Session, select

from app.models import Reminder, ScheduledInvoice, init_db, make_engine, utcnow

REPO = Path(__file__).resolve().parents[1]


def _run(code: str, cwd: Path, **env: str) -> str:
    """Run `code` as a script file in a fresh Python, from `cwd`; return its stdout.

    A script file, not `python -c`: python-dotenv treats a run without a main
    file as interactive and searches from the working directory, which would
    hide where a server started by uvicorn reads .env from.
    """
    script = cwd.parent / f"{cwd.name}-script.py"
    script.write_text(code + "\n")
    clean = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
    clean.update(PYTHONPATH=str(REPO), PYTHONNOUSERSITE="1", **env)
    result = subprocess.run(
        [sys.executable, str(script)], cwd=cwd, env=clean, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_importing_the_web_module_leaves_claims_alone(tmp_path):
    url = f"sqlite:///{tmp_path / 'live.db'}"
    engine = make_engine(url)
    init_db(engine)
    with Session(engine) as session:
        session.add(Reminder(invoice_id=1, draft_text="Paying soon?", status="SENDING"))
        session.add(ScheduledInvoice(schedule_id=1, sequence=1, amount_cents=100, issue_on=utcnow(), status="SENDING"))
        session.commit()

    out = _run("import app.web; print(hasattr(app.web, 'app'))", tmp_path, DATABASE_URL=url)

    with Session(engine) as session:
        assert session.exec(select(Reminder.status)).one() == "SENDING"
        assert session.exec(select(ScheduledInvoice.status)).one() == "SENDING"
    assert out == "False"  # only app.main builds the served app


def test_importing_the_web_module_creates_no_database(tmp_path):
    path = tmp_path / "none.db"
    _run("import app.web", tmp_path, DATABASE_URL=f"sqlite:///{path}")
    assert not path.exists()


def test_env_file_is_read_from_the_working_directory(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / ".env").write_text("DATABASE_URL=sqlite:///./from-cwd.db\n")
    out = _run("from app.config import get_settings; print(get_settings().database_url)", work)
    assert out == "sqlite:///./from-cwd.db"
