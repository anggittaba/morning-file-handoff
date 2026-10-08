"""Run a simulated morning end to end against real local SFTP servers, and measure it.

Times inside the simulation are simulated (06:00, 06:05, ...). Each pass does real SSH
logins, listings, downloads, uploads and read-backs over localhost; its duration is
measured with a real clock.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time as _time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

from . import classify, db
from . import generator as gen
from .engine import Engine
from .scheduler import pass_times
from .settings import Settings
from .sftp_farm import SftpFarm, load_or_create_host_key
from .topology import Topology


@dataclass
class SimEnv:
    settings: Settings
    topo: Topology
    scenario: gen.Scenario
    farm: SftpFarm | None = None
    applied_until: datetime | None = None

    @property
    def roots(self) -> Path:
        return self.settings.sim / "roots"

    async def start(self, at: datetime) -> Topology:
        sim = self.settings.sim
        if self.roots.exists():
            shutil.rmtree(self.roots)
        gen.prepare_dirs(sim, self.topo)
        server_pw, client_pw = gen.make_credentials(self.topo, self.scenario)
        (sim / "server_credentials.json").write_text(json.dumps(server_pw, indent=1))
        self.settings.credentials.parent.mkdir(parents=True, exist_ok=True)
        self.settings.credentials.write_text(json.dumps(client_pw, indent=1))
        self.client_credentials = client_pw
        key = load_or_create_host_key(sim / "host_key")
        self.farm = SftpFarm(self.topo, self.roots, server_pw, key, self.scenario.behaviours)
        offline_now = {n for n, (a, b) in self.scenario.offline.items() if a <= at < b}
        await self.farm.start(skip=offline_now)
        # endpoints that start offline still need a port in known_hosts; bind them briefly
        for name in offline_now:
            await self.farm.start_endpoint(name)
            await self.farm.stop_endpoint(name)
        live = self.farm.topology()
        self.farm.write_known_hosts(self.settings.known_hosts, live)
        gen.write_summary(sim / "scenario.json", self.scenario)
        await self.advance(at)
        return live

    async def advance(self, now: datetime) -> list[gen.Drop]:
        due = self.scenario.drops_due(self.applied_until, now)
        gen.apply_drops(self.roots, self.topo, due)
        self.applied_until = now
        for name, (a, b) in self.scenario.offline.items():
            if a <= now < b and name in self.farm.acceptors:
                await self.farm.stop_endpoint(name)
            elif not (a <= now < b) and name not in self.farm.acceptors:
                await self.farm.start_endpoint(name)
        return due

    async def stop(self) -> None:
        if self.farm:
            await self.farm.stop()


def fresh_settings(var: Path, **kw) -> Settings:
    return Settings(var=var, db=var / "handoff.db", credentials=var / "sim" / "credentials.json",
                    known_hosts=var / "sim" / "known_hosts", **kw)


async def run_morning(settings: Settings, topo: Topology, scenario: gen.Scenario, provider=None,
                      until: datetime | None = None, fixed_ports: bool = False, log=print) -> dict:
    """Fresh database and staging folders, then every pass of the window in simulated time."""
    if settings.db.exists():
        settings.db.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(settings.db) + suffix)
        if p.exists():
            p.unlink()
    day = scenario.day
    times = pass_times(day, settings.window_start, settings.window_end, settings.interval_min)
    if until is not None:
        times = [t for t in times if t <= until]
    if not fixed_ports:
        topo = topo.with_ports({ep.name: 0 for ep in topo.all_endpoints()})
    env = SimEnv(settings, topo, scenario)
    live = await env.start(times[0])
    con = db.connect(settings.db)
    engine = Engine(con, live, settings, env.client_credentials, provider)
    t_all = _time.perf_counter()
    try:
        for t in times:
            await env.advance(t)
            res = await engine.run_pass(t, "simulated")
            if log:
                log(f"{t:%H:%M}  {res['wall_ms']:>5} ms  found {res['found']:>2}  sent {res['sent']:>2}  "
                    f"scheduled {res['scheduled']:>2}  new failures {res['failed']:>2}  "
                    f"done {res['done_total']:>2}/{len(live.branches)}")
    finally:
        await env.stop()
    wall_total = _time.perf_counter() - t_all
    return measure(con, live, scenario, settings, times, wall_total, env.farm)


EXTRA_ALLOWED = {"missing": {"late"}}


def observed_categories(con, day: date, branch_id: str) -> list[str]:
    cats = []
    for e in db.events(con, day, branch_id):
        if e["outcome"] in ("fail", "late"):
            cats.append(classify.classify(e["stage"], e["detail"]).category)
    return cats


def measure(con, topo: Topology, scenario: gen.Scenario, settings: Settings, times: list[datetime],
            wall_total: float, farm: SftpFarm | None) -> dict:
    day = scenario.day
    deadline = datetime.combine(day, time(*settings.deadline))
    rows = {r["branch_id"]: r for r in db.day_rows(con, day)}
    runs = list(reversed(db.runs(con, day)))
    scheduled = {b: r for b, r in rows.items() if r["status"] == "scheduled"}
    by_deadline = {b: r for b, r in scheduled.items() if r["scheduled_at"] <= db.ts(deadline)}
    last_done = max((r["scheduled_at"] for r in scheduled.values()), default=None)
    clean = [b.branch_id for b in topo.branches if b.branch_id not in scenario.injected]
    clean_done = [b for b in clean if b in scheduled]
    clean_last = max((scheduled[b]["scheduled_at"] for b in clean_done), default=None)

    checks, correct = [], 0
    for bid, label in sorted(scenario.injected.items()):
        expected = gen.INJECTABLE[label]
        seen = observed_categories(con, day, bid)
        distinct = list(dict.fromkeys(seen))
        ok = expected in distinct and set(distinct) - {expected} <= EXTRA_ALLOWED.get(expected, set())
        correct += ok
        checks.append({"branch": bid, "injected": label, "expected": expected, "classified": distinct, "correct": ok,
                       "final_status": rows[bid]["status"]})
    false_alarms = {b: list(dict.fromkeys(observed_categories(con, day, b))) for b in clean}
    false_alarms = {b: c for b, c in false_alarms.items() if c}
    caught = Counter()
    for c in checks:
        for cat in c["classified"]:
            caught[cat] += 1
    deliveries = sum(s.get("deliveries", 0) for s in farm.stats.values()) if farm else None
    return {
        "day": day.isoformat(),
        "branches": len(topo.branches),
        "passes": len(runs),
        "pass_times": [r["started_at"][11:16] for r in runs],
        "scheduled_total": len(scheduled),
        "scheduled_by_deadline": len(by_deadline),
        "last_scheduled_at": last_done,
        "clean_branches": len(clean),
        "clean_scheduled": len(clean_done),
        "clean_last_scheduled_at": clean_last,
        "first_pass": {"at": runs[0]["started_at"][11:16], "scheduled": runs[0]["scheduled"],
                       "wall_ms": runs[0]["wall_ms"]} if runs else None,
        "wall_ms_per_pass": {"total": sum(r["wall_ms"] for r in runs), "max": max(r["wall_ms"] for r in runs),
                             "median": sorted(r["wall_ms"] for r in runs)[len(runs) // 2]},
        "wall_seconds_whole_run": round(wall_total, 2),
        "final_status": dict(Counter(r["status"] for r in rows.values())),
        "needs_a_person": sorted(b for b, r in rows.items() if r["status"] in ("failed", "late")),
        "injected": len(checks),
        "classified_correctly": correct,
        "classification": checks,
        "false_alarms_on_clean_branches": false_alarms,
        "failures_caught_by_category": dict(caught),
        "destination_deliveries_counted_by_servers": deliveries,
        "duplicate_jobs": con.execute("SELECT COUNT(*) - COUNT(DISTINCT branch_id) FROM jobs WHERE business_date=?",
                                      (day.isoformat(),)).fetchone()[0],
    }


async def run_batch(base_var: Path, topo: Topology, mornings: int, seed: int, day: date, log=print) -> dict:
    totals = Counter()
    per_label = Counter()
    per_label_ok = Counter()
    wrong = []
    for i in range(mornings):
        var = base_var / f"m{i:02d}"
        settings = fresh_settings(var, )
        sc = gen.random_scenario(day, topo, seed + i)
        res = await run_morning(settings, topo, sc, log=None)
        totals["mornings"] += 1
        totals["injected"] += res["injected"]
        totals["correct"] += res["classified_correctly"]
        totals["false_alarm_branches"] += len(res["false_alarms_on_clean_branches"])
        totals["clean_branches"] += res["clean_branches"]
        totals["clean_scheduled"] += res["clean_scheduled"]
        totals["duplicate_jobs"] += res["duplicate_jobs"]
        for c in res["classification"]:
            per_label[c["injected"]] += 1
            per_label_ok[c["injected"]] += c["correct"]
            if not c["correct"]:
                wrong.append({"morning": i, **c})
        if log:
            log(f"morning {i + 1:>2}/{mornings}: injected {res['injected']}, correct {res['classified_correctly']}, "
                f"clean delivered {res['clean_scheduled']}/{res['clean_branches']}")
        shutil.rmtree(var, ignore_errors=True)
    return {"mornings": mornings, "seed": seed, **totals,
            "per_label": {k: {"injected": per_label[k], "correct": per_label_ok[k]} for k in sorted(per_label)},
            "misclassified": wrong}
