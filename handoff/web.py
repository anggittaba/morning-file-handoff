"""Dashboard: one server-rendered screen for the whole morning, plus per-branch detail and a run log."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, time
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import actions, classify, db, explain
from .clock import CLOCK
from .engine import Engine
from .scheduler import next_pass, window_loop

HERE = Path(__file__).parent
NEEDS_PERSON = ("failed", "late")
DONE = ("scheduled", "handled", "skipped")
STATUS_LABEL = {"waiting": "Waiting", "late": "Late", "found": "Found", "sent": "Sent", "scheduled": "Scheduled",
                "failed": "Failed", "handled": "Handled", "skipped": "Skipped"}
HEALTH_LABEL = {"ok": "ok", "auth_failed": "login refused", "disk_full": "disk full", "no_write": "no write access",
                "unreachable": "unreachable", None: "not checked yet"}
HEALTH_SHORT = {"ok": "ok", "auth_failed": "login", "disk_full": "disk full", "no_write": "no write",
                "unreachable": "down", None: "unchecked"}
FILTERS = {"human": ("Needs a person", NEEDS_PERSON), "open": ("Still moving", ("waiting", "found", "sent")),
           "done": ("Done", DONE)}


def create_app(engine: Engine, loop: bool = True, sim_env=None) -> FastAPI:
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(STATUS_LABEL=STATUS_LABEL, HEALTH_LABEL=HEALTH_LABEL, HEALTH_SHORT=HEALTH_SHORT)
    templates.env.filters["hm"] = lambda v: (v or "")[11:16]
    templates.env.filters["fromjson"] = lambda v: json.loads(v or "[]")
    templates.env.filters["thousands"] = lambda v: f"{v:,}" if isinstance(v, int) else (v or "—")
    stop = asyncio.Event()
    tasks: list[asyncio.Task] = []
    con, topo, settings = engine.con, engine.topo, engine.settings

    async def run(now: datetime, trigger: str, only: set[str] | None = None) -> dict:
        if sim_env is not None:
            await sim_env.advance(now)
        return await engine.run_pass(now, trigger, only)

    async def drops_loop() -> None:  # demo only: files keep arriving on the simulated clock
        while not stop.is_set():
            await sim_env.advance(CLOCK.now())
            try:
                await asyncio.wait_for(stop.wait(), 1.0)
            except asyncio.TimeoutError:
                pass

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if loop and not CLOCK.frozen:
            tasks.append(asyncio.create_task(window_loop(run, CLOCK, settings, stop)))
        if sim_env is not None and not CLOCK.frozen:
            tasks.append(asyncio.create_task(drops_loop()))
        yield
        stop.set()
        for t in tasks:
            t.cancel()

    app = FastAPI(title="Morning File Handoff", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def base_ctx(request: Request, active: str) -> dict:
        now = CLOCK.now()
        day = now.date()
        db.ensure_day(con, day, [b.branch_id for b in topo.branches], now)
        rows = db.day_rows(con, day)
        counts = Counter(r["status"] for r in rows)
        deadline = engine.deadline(day)
        left = (deadline - now).total_seconds()
        need = sum(counts[s] for s in NEEDS_PERSON)
        open_n = len(rows) - sum(counts[s] for s in DONE)
        at_risk = 0 < left <= settings.at_risk_min * 60 and need > 0
        runs = db.runs(con, day)
        nxt = next_pass(now, settings.window_start, settings.window_end, settings.interval_min)
        return {"request": request, "active": active, "now": now, "day": day, "rows": rows, "counts": counts,
                "deadline": deadline, "seconds_left": int(left), "need": need, "open_n": open_n, "at_risk": at_risk,
                "total": len(rows), "last_run": runs[0] if runs else None, "next_pass": nxt,
                "frozen": CLOCK.frozen, "interval": settings.interval_min, "speed": CLOCK.speed if CLOCK.start else 1,
                "flash": request.query_params.get("msg"), "error": request.query_params.get("err")}

    def explanation_title(day, branch_id) -> tuple[str | None, object]:
        exp = db.latest_explanation(con, day, branch_id)
        return (classify.TEMPLATES[exp["rule_category"]].title if exp else None), exp

    @app.get("/", response_class=HTMLResponse)
    def board(request: Request, f: str | None = None):
        ctx = base_ctx(request, "human" if f == "human" else "board")
        day = ctx["day"]
        health = db.health(con)
        tiles = []
        for b, r in zip(topo.branches, ctx["rows"]):
            title, _ = explanation_title(day, b.branch_id)
            h = health.get(b.destination)
            tiles.append({"b": b, "r": r, "problem": title if r["status"] in NEEDS_PERSON else None,
                          "dest_health": h["status"] if h else None,
                          "needs": r["status"] in NEEDS_PERSON})
        if f in FILTERS:
            tiles = [t for t in tiles if t["r"]["status"] in FILTERS[f][1]]
        staging = [{"ep": ep, "h": health.get(ep.name)} for ep in topo.staging]
        dest_counts = Counter((health.get(n)["status"] if health.get(n) else None) for n in topo.destinations)
        return templates.TemplateResponse(request, "board.html", {
            **ctx, "tiles": tiles, "f": f, "filters": FILTERS, "staging": staging, "dest_counts": dest_counts})

    @app.get("/b/{branch_id}", response_class=HTMLResponse)
    def detail(request: Request, branch_id: str):
        ctx = base_ctx(request, "board")
        try:
            b = topo.branch(branch_id)
        except KeyError:
            raise HTTPException(404, "unknown branch")
        day = ctx["day"]
        r = db.get(con, day, branch_id)
        title, exp = explanation_title(day, branch_id)
        show_exp = exp is not None and r["status"] in NEEDS_PERSON
        job = con.execute("SELECT * FROM jobs WHERE business_date=? AND branch_id=?",
                          (day.isoformat(), branch_id)).fetchone()
        h = db.health(con).get(b.destination)
        return templates.TemplateResponse(request, "detail.html", {
            **ctx, "b": b, "r": r, "exp": exp if show_exp else None, "past_exp": exp if not show_exp else None,
            "title": title, "events": db.events(con, day, branch_id), "job": job, "dest_health": h,
            "template": classify.TEMPLATES[exp["rule_category"]] if exp else None})

    def back(branch_id: str, msg: str = "", err: str = "") -> RedirectResponse:
        q = f"?msg={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
        return RedirectResponse(f"/b/{branch_id}{q}", status_code=303)

    @app.post("/b/{branch_id}/retry")
    async def retry(branch_id: str, note: str = Form("")):
        now = CLOCK.now()
        try:
            actions.authorise_retry(con, now.date(), branch_id, settings.operator, now, note)
        except actions.ActionError as exc:
            return back(branch_id, err=str(exc))
        await run(now, "retry", {branch_id})
        r = db.get(con, now.date(), branch_id)
        return back(branch_id, msg=f"Retried now: {STATUS_LABEL[r['status']].lower()}.")

    @app.post("/b/{branch_id}/handled")
    def handled(branch_id: str, note: str = Form("")):
        now = CLOCK.now()
        try:
            actions.mark_handled(con, now.date(), branch_id, settings.operator, now, note)
        except actions.ActionError as exc:
            return back(branch_id, err=str(exc))
        return back(branch_id)

    @app.post("/b/{branch_id}/skip")
    def skip(branch_id: str, reason: str = Form("")):
        now = CLOCK.now()
        try:
            actions.skip(con, now.date(), branch_id, settings.operator, now, reason)
        except actions.ActionError as exc:
            return back(branch_id, err=str(exc))
        return back(branch_id)

    @app.post("/b/{branch_id}/reopen")
    def reopen(branch_id: str):
        now = CLOCK.now()
        try:
            actions.reopen(con, now.date(), branch_id, settings.operator, now)
        except actions.ActionError as exc:
            return back(branch_id, err=str(exc))
        return back(branch_id)

    @app.post("/run")
    async def run_now():
        res = await run(CLOCK.now(), "manual")
        msg = f"Pass finished in {res['wall_ms']} ms."
        return RedirectResponse(f"/?msg={quote(msg)}", status_code=303)

    @app.get("/runs", response_class=HTMLResponse)
    def run_log(request: Request):
        ctx = base_ctx(request, "runs")
        day = ctx["day"]
        cats = Counter()
        for r in ctx["rows"]:
            if r["status"] in NEEDS_PERSON:
                exp = db.latest_explanation(con, day, r["branch_id"])
                if exp:
                    cats[exp["rule_category"]] += 1
        recovered = con.execute(
            "SELECT COUNT(DISTINCT branch_id) FROM events WHERE business_date=? AND outcome='fail' AND branch_id IN "
            "(SELECT branch_id FROM branch_day WHERE business_date=? AND status='scheduled')",
            (day.isoformat(), day.isoformat())).fetchone()[0]
        runs = db.runs(con, day)
        deadline_ts = db.ts(ctx["deadline"])
        sched = [r for r in ctx["rows"] if r["status"] == "scheduled"]
        glance = {"by_deadline": sum(1 for r in sched if r["scheduled_at"] <= deadline_ts),
                  "last_delivery": max((r["scheduled_at"] for r in sched), default=None),
                  "engine_seconds": sum(r["wall_ms"] or 0 for r in runs) / 1000, "passes": len(runs)}
        return templates.TemplateResponse(request, "runs.html", {
            **ctx, "runs": runs, "glance": glance, "draft": db.latest_draft(con, day),
            "cats": [(classify.TEMPLATES[c].title, n) for c, n in cats.most_common()], "recovered": recovered,
            "recent": list(reversed(db.events(con, day, limit=40)))})

    @app.post("/runs/draft")
    async def redraft():
        now = CLOCK.now()
        await explain.draft_summary(con, engine.provider, now.date(), engine.names(), now, engine.deadline(now.date()))
        return RedirectResponse("/runs?msg=" + quote("New draft written. Nothing was sent."), status_code=303)

    @app.get("/scheduler", response_class=HTMLResponse)
    def jobs_page(request: Request):
        ctx = base_ctx(request, "scheduler")
        return templates.TemplateResponse(request, "scheduler.html", {**ctx, "jobs": db.jobs(con, ctx["day"])})

    @app.get("/api/board")
    def api_board():
        now = CLOCK.now()
        rows = db.day_rows(con, now.date())
        return JSONResponse({"now": now.isoformat(), "deadline": engine.deadline(now.date()).isoformat(),
                             "branches": [{k: r[k] for k in ("branch_id", "status", "source", "file_name",
                                                             "failure_stage", "failure_detail")} for r in rows]})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
