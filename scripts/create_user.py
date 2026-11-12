"""Add a Duebell user from the command line (issue #24, ADR 0008).

Whoever runs the server adds users here. Visitors can also sign up at
/signup while SIGNUP_ENABLED is on (issue #150), under the same limits:

    .venv/bin/python scripts/create_user.py you@example.com
    .venv/bin/python scripts/create_user.py you@example.com --claim

The password is read from a prompt, or from DUEBELL_PASSWORD when set.
--claim gives the user every record that has no owner, which is how a
database from before sign-in existed gets one. With --claim, an email that
already has a user is not created again and no password is asked for; that
user gets the records (issue #123). Without --claim, an existing email stops
the script.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, SQLModel, select

from app.auth import AuthError, create_user, normalize_email
from app.config import get_settings
from app.models import User, init_db, make_engine


def claim_ownerless(session: Session, user_id: int) -> dict[str, int]:
    claimed = {}
    for table in SQLModel.metadata.sorted_tables:
        if "user_id" not in table.columns:
            continue
        result = session.execute(
            table.update().where(table.c.user_id.is_(None)).values(user_id=user_id)
        )
        if result.rowcount:
            claimed[table.name] = result.rowcount
    session.commit()
    return claimed


def main() -> None:
    parser = argparse.ArgumentParser(description="Add a Duebell user.")
    parser.add_argument("email")
    parser.add_argument("--claim", action="store_true", help="Give this user every record with no owner.")
    args = parser.parse_args()

    engine = make_engine(get_settings().database_url)
    init_db(engine)
    with Session(engine) as session:
        user = None
        if args.claim:
            email = normalize_email(args.email)
            user = session.exec(select(User).where(User.email == email)).first()
        if user is None:
            password = os.getenv("DUEBELL_PASSWORD") or getpass.getpass("Password: ")
            try:
                user = create_user(session, args.email, password)
            except AuthError as exc:
                raise SystemExit(str(exc))
            print(f"Created {user.email}.")
        if args.claim:
            claimed = claim_ownerless(session, user.id)
            summary = ", ".join(f"{count} {name}" for name, count in claimed.items()) or "nothing"
            print(f"Claimed {summary}.")


if __name__ == "__main__":
    main()
