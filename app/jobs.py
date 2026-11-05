"""Background jobs for Duebell (issues #15 and #22).

The reminder scan only drafts. Sending still needs a human approval
(ADR 0003). The risk refit only stores a comparison; the rule baseline stays
the shipped score (ADR 0002).
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from app.learning import refit
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

    def refit_tick() -> None:
        try:
            with Session(app.state.engine) as session:
                refit(session)
        except Exception:  # a failed refit must not stop the scheduler
            log.exception("risk refit failed")

    scheduler.add_job(
        refit_tick,
        "interval",
        hours=6,
        id="risk-refit",
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
