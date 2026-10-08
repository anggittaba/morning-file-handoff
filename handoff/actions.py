"""What a person can do from the dashboard. Every action is written to the audit log."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime

from . import db


class ActionError(ValueError):
    pass


def _row(con: sqlite3.Connection, day: date, branch_id: str) -> sqlite3.Row:
    row = db.get(con, day, branch_id)
    if row is None:
        raise ActionError(f"no record for {branch_id} on {day}")
    return row


def authorise_retry(con: sqlite3.Connection, day: date, branch_id: str, actor: str, now: datetime,
                    note: str = "") -> None:
    """Allow one more attempt. Needed after login, disk-full and permission errors; harmless otherwise."""
    row = _row(con, day, branch_id)
    if row["status"] not in ("failed", "late"):
        raise ActionError(f"{branch_id} is {row['status']}; there is nothing to retry")
    db.update(con, day, branch_id, now, retry_authorized=1)
    db.event(con, now, day, branch_id, "human", "retry", note or "retry authorised", actor=actor)


def mark_handled(con: sqlite3.Connection, day: date, branch_id: str, actor: str, now: datetime, note: str) -> None:
    """The file was dealt with outside the engine (for example sent by hand). Closes the branch for today."""
    if not note.strip():
        raise ActionError("say how it was handled")
    row = _row(con, day, branch_id)
    if row["status"] in db.TERMINAL:
        raise ActionError(f"{branch_id} is already {row['status']}")
    db.update(con, day, branch_id, now, status="handled", note=note.strip(), closed_at=db.ts(now), retry_authorized=0)
    db.event(con, now, day, branch_id, "human", "handled", note.strip(), actor=actor)


def skip(con: sqlite3.Connection, day: date, branch_id: str, actor: str, now: datetime, reason: str) -> None:
    """No file will be delivered today, for a stated reason (for example the branch was closed)."""
    if not reason.strip():
        raise ActionError("a reason is required to skip")
    row = _row(con, day, branch_id)
    if row["status"] in db.TERMINAL:
        raise ActionError(f"{branch_id} is already {row['status']}")
    db.update(con, day, branch_id, now, status="skipped", note=reason.strip(), closed_at=db.ts(now), retry_authorized=0)
    db.event(con, now, day, branch_id, "human", "skipped", reason.strip(), actor=actor)


def reopen(con: sqlite3.Connection, day: date, branch_id: str, actor: str, now: datetime) -> None:
    """Undo a handled or skipped decision; the engine looks at the branch again on the next pass."""
    row = _row(con, day, branch_id)
    if row["status"] not in ("handled", "skipped"):
        raise ActionError(f"{branch_id} is {row['status']}; only handled or skipped branches can be reopened")
    db.update(con, day, branch_id, now, status="failed" if row["failure_detail"] else "waiting", closed_at=None,
              note=None)
    db.event(con, now, day, branch_id, "human", "reopened", f"was {row['status']}", actor=actor)
