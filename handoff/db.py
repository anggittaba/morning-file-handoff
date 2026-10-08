"""SQLite storage. The audit log (events) is append-only, enforced by triggers."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS branch_day (
  business_date TEXT NOT NULL,
  branch_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'waiting',
  source TEXT, file_name TEXT, size INTEGER, rows INTEGER, sha256 TEXT,
  seen_sig TEXT,
  sent_sha TEXT, remote_path TEXT,
  failure TEXT, failure_detail TEXT, failure_stage TEXT, failure_since TEXT,
  human_gate INTEGER NOT NULL DEFAULT 0,
  retry_authorized INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  found_at TEXT, sent_at TEXT, scheduled_at TEXT, closed_at TEXT, updated_at TEXT,
  PRIMARY KEY (business_date, branch_id)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  business_date TEXT NOT NULL,
  branch_id TEXT,
  run_id INTEGER,
  stage TEXT NOT NULL,
  outcome TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  file_name TEXT,
  actor TEXT NOT NULL DEFAULT 'engine'
);
CREATE INDEX IF NOT EXISTS events_branch ON events (business_date, branch_id, id);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_date TEXT NOT NULL,
  started_at TEXT NOT NULL,
  trigger TEXT NOT NULL,
  wall_ms INTEGER,
  found INTEGER DEFAULT 0, sent INTEGER DEFAULT 0, scheduled INTEGER DEFAULT 0,
  failed INTEGER DEFAULT 0, done_total INTEGER DEFAULT 0, open_total INTEGER DEFAULT 0,
  errors TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_date TEXT NOT NULL,
  branch_id TEXT NOT NULL,
  file_name TEXT NOT NULL,
  destination TEXT NOT NULL,
  remote_path TEXT NOT NULL,
  run_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  created_at TEXT NOT NULL,
  UNIQUE (business_date, branch_id)
);
CREATE TABLE IF NOT EXISTS health (
  endpoint TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  detail TEXT DEFAULT '',
  latency_ms INTEGER,
  checked_at TEXT,
  last_ok_at TEXT
);
CREATE TABLE IF NOT EXISTS explanations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_date TEXT NOT NULL,
  branch_id TEXT NOT NULL,
  signature TEXT NOT NULL,
  rule_category TEXT NOT NULL,
  rule_name TEXT NOT NULL,
  engine TEXT NOT NULL,
  explanation TEXT NOT NULL,
  likely_cause TEXT NOT NULL,
  next_step TEXT NOT NULL,
  decided_by TEXT NOT NULL,
  llm_error TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_date TEXT NOT NULL,
  created_at TEXT NOT NULL,
  engine TEXT NOT NULL,
  text TEXT NOT NULL,
  llm_error TEXT
);
"""

TERMINAL = {"scheduled", "handled", "skipped"}
STATUSES = ["waiting", "late", "found", "sent", "scheduled", "failed", "handled", "skipped"]


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


def ts(when: datetime) -> str:
    return when.replace(microsecond=0).isoformat()


def ensure_day(con: sqlite3.Connection, day: date, branch_ids: list[str], when: datetime) -> None:
    con.executemany(
        "INSERT OR IGNORE INTO branch_day (business_date, branch_id, updated_at) VALUES (?, ?, ?)",
        [(day.isoformat(), b, ts(when)) for b in branch_ids])


def get(con: sqlite3.Connection, day: date, branch_id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM branch_day WHERE business_date=? AND branch_id=?",
                       (day.isoformat(), branch_id)).fetchone()


def day_rows(con: sqlite3.Connection, day: date) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM branch_day WHERE business_date=? ORDER BY branch_id",
                       (day.isoformat(),)).fetchall()


def update(con: sqlite3.Connection, day: date, branch_id: str, when: datetime, **fields) -> None:
    fields["updated_at"] = ts(when)
    cols = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE branch_day SET {cols} WHERE business_date=? AND branch_id=?",
                [*fields.values(), day.isoformat(), branch_id])


def event(con: sqlite3.Connection, when: datetime, day: date, branch_id: str | None, stage: str, outcome: str,
          detail: str = "", run_id: int | None = None, file_name: str | None = None, actor: str = "engine") -> int:
    cur = con.execute(
        "INSERT INTO events (ts, business_date, branch_id, run_id, stage, outcome, detail, file_name, actor) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts(when), day.isoformat(), branch_id, run_id, stage, outcome, detail, file_name, actor))
    return cur.lastrowid


def events(con: sqlite3.Connection, day: date, branch_id: str | None = None, limit: int | None = None) -> list:
    q = "SELECT * FROM events WHERE business_date=?"
    args: list = [day.isoformat()]
    if branch_id:
        q += " AND branch_id=?"
        args.append(branch_id)
    q += " ORDER BY id"
    if limit:
        q = f"SELECT * FROM ({q.replace('ORDER BY id', 'ORDER BY id DESC')} LIMIT {int(limit)}) ORDER BY id"
    return con.execute(q, args).fetchall()


def set_health(con: sqlite3.Connection, endpoint: str, kind: str, status: str, detail: str, latency_ms: int | None,
               when: datetime) -> None:
    con.execute(
        "INSERT INTO health (endpoint, kind, status, detail, latency_ms, checked_at, last_ok_at) "
        "VALUES (?, ?, ?, ?, ?, ?, CASE WHEN ?='ok' THEN ? END) "
        "ON CONFLICT(endpoint) DO UPDATE SET status=excluded.status, detail=excluded.detail, "
        "latency_ms=excluded.latency_ms, checked_at=excluded.checked_at, "
        "last_ok_at=COALESCE(excluded.last_ok_at, health.last_ok_at)",
        (endpoint, kind, status, detail, latency_ms, ts(when), status, ts(when)))


def health(con: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["endpoint"]: r for r in con.execute("SELECT * FROM health")}


def start_run(con: sqlite3.Connection, day: date, when: datetime, trigger: str) -> int:
    return con.execute("INSERT INTO runs (business_date, started_at, trigger) VALUES (?, ?, ?)",
                       (day.isoformat(), ts(when), trigger)).lastrowid


def finish_run(con: sqlite3.Connection, run_id: int, wall_ms: int, counts: dict, errors: list[str]) -> None:
    con.execute("UPDATE runs SET wall_ms=?, found=?, sent=?, scheduled=?, failed=?, done_total=?, open_total=?, "
                "errors=? WHERE id=?",
                (wall_ms, counts.get("found", 0), counts.get("sent", 0), counts.get("scheduled", 0),
                 counts.get("failed", 0), counts.get("done_total", 0), counts.get("open_total", 0),
                 json.dumps(errors), run_id))


def runs(con: sqlite3.Connection, day: date) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM runs WHERE business_date=? ORDER BY id DESC", (day.isoformat(),)).fetchall()


def latest_explanation(con: sqlite3.Connection, day: date, branch_id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM explanations WHERE business_date=? AND branch_id=? ORDER BY id DESC LIMIT 1",
                       (day.isoformat(), branch_id)).fetchone()


def latest_draft(con: sqlite3.Connection, day: date) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM drafts WHERE business_date=? ORDER BY id DESC LIMIT 1",
                       (day.isoformat(),)).fetchone()


def jobs(con: sqlite3.Connection, day: date) -> list[sqlite3.Row]:
    return con.execute("SELECT * FROM jobs WHERE business_date=? ORDER BY run_at, branch_id",
                       (day.isoformat(),)).fetchall()
