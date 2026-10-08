"""Built-in scheduler queue (replaces clicking each delivered file into a separate scheduler app)
and the loop that runs the engine every few minutes inside the morning window."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import date, datetime, time, timedelta

from . import db

log = logging.getLogger("handoff.scheduler")


def enqueue(con: sqlite3.Connection, day: date, branch_id: str, file_name: str, destination: str,
            remote_path: str, slot: str, now: datetime) -> tuple[sqlite3.Row, bool]:
    """Queue the destination job for this branch's file. Returns (job, created). Never queues twice."""
    hh, mm = (int(x) for x in slot.split(":"))
    run_at = datetime.combine(day, time(hh, mm))
    cur = con.execute(
        "INSERT OR IGNORE INTO jobs (business_date, branch_id, file_name, destination, remote_path, run_at, status, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
        (day.isoformat(), branch_id, file_name, destination, remote_path, db.ts(run_at), db.ts(now)))
    job = con.execute("SELECT * FROM jobs WHERE business_date=? AND branch_id=?",
                      (day.isoformat(), branch_id)).fetchone()
    return job, cur.rowcount == 1


def mark_due(con: sqlite3.Connection, now: datetime) -> int:
    """Jobs whose time has come are marked due. A real deployment would hand them to its scheduler here."""
    return con.execute("UPDATE jobs SET status='due' WHERE status='queued' AND run_at<=?", (db.ts(now),)).rowcount


def pass_times(day: date, start: tuple[int, int], end: tuple[int, int], interval_min: float) -> list[datetime]:
    t = datetime.combine(day, time(*start))
    stop = datetime.combine(day, time(*end))
    out = []
    while t <= stop:
        out.append(t)
        t += timedelta(minutes=interval_min)
    return out


def next_pass(now: datetime, start: tuple[int, int], end: tuple[int, int], interval_min: float) -> datetime | None:
    for t in pass_times(now.date(), start, end, interval_min):
        if t > now:
            return t
    return None


async def window_loop(run, clock, settings, stop: asyncio.Event) -> None:
    """Call `run(now)` at every pass time between window start and end, on the app clock."""
    while not stop.is_set():
        now = clock.now()
        nxt = next_pass(now, settings.window_start, settings.window_end, settings.interval_min)
        if nxt is None:
            tomorrow = datetime.combine(now.date() + timedelta(days=1), time(*settings.window_start))
            nxt = tomorrow
        wait = max(0.0, clock.sleep_seconds((nxt - now).total_seconds()))
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await run(clock.now(), "schedule")
        except Exception:  # keep the loop alive; the error is in the log and the run table
            log.exception("scheduled pass failed")
