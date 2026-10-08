"""Explanations for people: per failed branch, and the 07:30 status draft.

The rule-based classifier always runs. If an LLM provider is configured, its text is shown
instead, with the rule category kept alongside. If the LLM call fails, the rule text is used.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date, datetime

from . import classify, db, llm


def _facts(con: sqlite3.Connection, day: date, row: sqlite3.Row, branch_name: str, now: datetime,
           deadline: datetime) -> dict:
    timeline = [f"{e['ts'][11:16]} {e['stage']} {e['outcome']}: {e['detail']}"
                for e in db.events(con, day, row["branch_id"], limit=12)]
    return {"branch": f"{row['branch_id']} {branch_name}", "status": row["status"], "now": now.strftime("%H:%M"),
            "deadline": deadline.strftime("%H:%M"), "stage": row["failure_stage"], "error": row["failure_detail"],
            "file": row["file_name"], "rows": row["rows"], "size_bytes": row["size"], "source": row["source"],
            "timeline": timeline}


async def ensure(con: sqlite3.Connection, provider, day: date, row: sqlite3.Row, branch_name: str,
                 now: datetime, deadline: datetime) -> sqlite3.Row | None:
    """Write an explanation if this failure (status + stage + error) has not been explained yet."""
    if row["status"] not in ("failed", "late"):
        return None
    sig = classify.signature(row["status"], row["failure_stage"], row["failure_detail"])
    last = db.latest_explanation(con, day, row["branch_id"])
    if last and last["signature"] == sig:
        return last
    result = classify.classify(row["failure_stage"], row["failure_detail"])
    t = result.template
    text = {"explanation": t.explanation, "likely_cause": t.likely_cause, "next_step": t.next_step}
    engine, error = "rules", None
    if provider is not None:
        facts = _facts(con, day, row, branch_name, now, deadline)
        facts["rule_category_hint"] = result.category
        try:
            out = await asyncio.wait_for(asyncio.to_thread(
                provider.complete, llm.EXPLAIN_PROMPT, json.dumps(facts, indent=1), llm.EXPLAIN_SCHEMA, "explain"), 40)
            text = llm.validate(out, llm.EXPLAIN_FIELDS)
            engine = provider.name
        except Exception as exc:  # fall back to the rule text, keep the reason
            error = f"{type(exc).__name__}: {exc}"[:300]
    con.execute(
        "INSERT INTO explanations (business_date, branch_id, signature, rule_category, rule_name, engine, explanation, "
        "likely_cause, next_step, decided_by, llm_error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (day.isoformat(), row["branch_id"], sig, result.category, result.rule, engine, text["explanation"],
         text["likely_cause"], text["next_step"], t.decided_by, error, db.ts(now)))
    return db.latest_explanation(con, day, row["branch_id"])


def summary_facts(con: sqlite3.Connection, day: date, names: dict[str, str], now: datetime, deadline: datetime) -> dict:
    rows = db.day_rows(con, day)
    done = [r for r in rows if r["status"] == "scheduled"]
    closed = [r for r in rows if r["status"] in ("handled", "skipped")]
    open_rows = [r for r in rows if r["status"] not in ("scheduled", "handled", "skipped")]
    issues = []
    for r in open_rows:
        exp = db.latest_explanation(con, day, r["branch_id"])
        title = classify.TEMPLATES[exp["rule_category"]].title if exp else r["status"]
        issues.append({"branch": f"{r['branch_id']} {names.get(r['branch_id'], '')}".strip(), "status": r["status"],
                       "problem": title})
    return {"time": now.strftime("%H:%M"), "deadline": deadline.strftime("%H:%M"), "total": len(rows),
            "scheduled": len(done), "closed_by_people": [f"{r['branch_id']} ({r['status']})" for r in closed],
            "needs_a_person": issues}


def template_summary(f: dict) -> str:
    lines = [f"Morning file handoff, {f['time']}: {f['scheduled']} of {f['total']} branches delivered and scheduled."]
    if f["closed_by_people"]:
        lines.append(f"Closed by the team: {', '.join(f['closed_by_people'])}.")
    if f["needs_a_person"]:
        lines.append(f"Still need a person ({len(f['needs_a_person'])}):")
        lines += [f"- {i['branch']}: {i['problem'].lower()}" for i in f["needs_a_person"]]
    else:
        lines.append("Nothing needs a person.")
    return "\n".join(lines)


async def draft_summary(con: sqlite3.Connection, provider, day: date, names: dict[str, str], now: datetime,
                        deadline: datetime) -> sqlite3.Row:
    """Draft the status message. It is stored and shown; nothing is ever sent."""
    facts = summary_facts(con, day, names, now, deadline)
    text, engine, error = template_summary(facts), "template", None
    if provider is not None:
        try:
            out = await asyncio.wait_for(asyncio.to_thread(
                provider.complete, llm.SUMMARY_PROMPT, json.dumps(facts, indent=1), llm.SUMMARY_SCHEMA, "summary"), 40)
            text, engine = llm.validate(out, ["summary"])["summary"], provider.name
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:300]
    con.execute("INSERT INTO drafts (business_date, created_at, engine, text, llm_error) VALUES (?, ?, ?, ?, ?)",
                (day.isoformat(), db.ts(now), engine, text, error))
    return db.latest_draft(con, day)
