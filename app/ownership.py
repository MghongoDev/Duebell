"""Who a query may see (issue #24, ADR 0008).

Every read takes a scope with no default: one user's id, or EVERY_USER for
the background scheduler. Passing None is an error, so a caller that forgets
the scope fails loudly instead of reading every user's records.
"""

from __future__ import annotations

from typing import Final


class _EveryUser:
    """The scheduler's scope. Routes never use it."""

    def __repr__(self) -> str:
        return "EVERY_USER"


EVERY_USER: Final = _EveryUser()
Scope = int | _EveryUser


def check_scope(scope: Scope) -> Scope:
    if scope is EVERY_USER or (isinstance(scope, int) and not isinstance(scope, bool)):
        return scope
    raise TypeError(f"A query needs a user id or EVERY_USER, not {scope!r}.")


def owned(query, model, scope: Scope):
    """Filter a select to the rows the scope may see."""
    if check_scope(scope) is EVERY_USER:
        return query
    return query.where(model.user_id == scope)


def get_owned(session, model, record_id, user_id: int):
    """Fetch one row for a user. Another user's row reads as missing."""
    check_scope(user_id)
    record = session.get(model, record_id)
    if record is None or record.user_id != user_id:
        return None
    return record
