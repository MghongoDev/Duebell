"""Background reminder scan for Duebell (issue #15).

The scheduler only drafts reminders. Sending still needs a human approval.
See ADR 0003.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from app.reminders import draft_reminders

log = logging.getLogger("duebell.jobs")


def start_scheduler(app) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")

    def tick() -> None:
        try:
            with Session(app.state.engine) as session:
                draft_reminders(session, app.state.settings)
        except Exception:  # the scheduler must survive a bad tick
            log.exception("reminder scan failed")

    scheduler.add_job(
        tick,
        "interval",
        minutes=30,
        id="reminder-scan",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    app.state.scheduler = scheduler
    return scheduler


def stop_scheduler(app) -> None:
    scheduler = getattr(app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.shutdown(wait=False)
        app.state.scheduler = None
