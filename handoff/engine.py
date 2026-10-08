"""The deterministic part: discover, validate, transfer, verify, schedule. One call = one pass.

A pass is safe to repeat. Finished branches are skipped, a file already at the destination
with the same checksum is never sent again, and the scheduler refuses a second job for the
same branch and day. Login, disk-full and permission errors stop and wait for a person.
"""

from __future__ import annotations

import asyncio
import posixpath
import sqlite3
import time as _time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import date, datetime, time

import asyncssh

from . import db, explain, scheduler
from . import exportfile as ef
from .settings import Settings
from .sftp_client import RemoteFile, TransferError, deliver, exists, list_dir, read_file, session
from .topology import Branch, Endpoint, Topology

GATED = {"auth": "login refused", "no_space": "disk full", "no_write": "no write permission"}


@dataclass
class Engine:
    con: sqlite3.Connection
    topo: Topology
    settings: Settings
    credentials: dict[str, str]
    provider: object | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def known_hosts(self) -> str | None:
        kh = self.settings.known_hosts
        return str(kh) if kh and kh.exists() else None

    def deadline(self, day: date) -> datetime:
        return datetime.combine(day, time(*self.settings.deadline))

    def names(self) -> dict[str, str]:
        return {b.branch_id: b.name for b in self.topo.branches}

    def _session(self, ep: Endpoint):
        return session(ep, self.credentials.get(ep.name), self.known_hosts, self.settings.connect_timeout_s)

    # ── one pass ────────────────────────────────────────────────────────────────────

    async def run_pass(self, now: datetime, trigger: str = "schedule", only: set[str] | None = None) -> dict:
        async with self.lock:
            return await self._run_pass(now, trigger, only)

    async def _run_pass(self, now: datetime, trigger: str, only: set[str] | None) -> dict:
        day = now.date()
        t0 = _time.perf_counter()
        con = self.con
        db.ensure_day(con, day, [b.branch_id for b in self.topo.branches], now)
        run_id = db.start_run(con, day, now, trigger)
        counts = {"found": 0, "sent": 0, "scheduled": 0, "failed": 0}
        errors: list[str] = []
        todo, watch = [], []
        for b in self.topo.branches:
            if only and b.branch_id not in only:
                continue
            row = db.get(con, day, b.branch_id)
            if row["status"] == "scheduled":
                watch.append(b)
                continue
            if row["status"] in db.TERMINAL:
                continue
            if row["status"] == "failed" and row["human_gate"] and not row["retry_authorized"]:
                continue
            todo.append(b)
        async with AsyncExitStack() as stack:
            sessions, index = await self._discover(stack, now, errors)
            if todo:
                await self._probe_destinations(todo, day, now)
            sem = asyncio.Semaphore(self.settings.concurrency)

            async def one(b: Branch) -> None:
                async with sem:
                    try:
                        await self._branch(b, day, now, run_id, sessions, index, counts)
                    except Exception as exc:  # one branch must never stop the others
                        errors.append(f"{b.branch_id}: {type(exc).__name__}: {exc}")
                        db.event(con, now, day, b.branch_id, "engine", "fail", f"{type(exc).__name__}: {exc}", run_id)

            await asyncio.gather(*(one(b) for b in todo))
            for b in watch:
                try:
                    await self._watch_delivered(b, day, now, run_id, sessions, index, counts)
                except Exception as exc:
                    errors.append(f"{b.branch_id}: {type(exc).__name__}: {exc}")
        scheduler.mark_due(con, now)
        rows = db.day_rows(con, day)
        for r in rows:
            name = self.names()[r["branch_id"]]
            await explain.ensure(con, self.provider, day, r, name, now, self.deadline(day))
        if now >= self.deadline(day):
            last = db.latest_draft(con, day)
            if last is None or last["created_at"] < db.ts(self.deadline(day)):
                await explain.draft_summary(con, self.provider, day, self.names(), now, self.deadline(day))
        counts["done_total"] = sum(1 for r in rows if r["status"] in db.TERMINAL)
        counts["open_total"] = len(rows) - counts["done_total"]
        wall_ms = int((_time.perf_counter() - t0) * 1000)
        db.finish_run(con, run_id, wall_ms, counts, errors)
        return {"run_id": run_id, "wall_ms": wall_ms, **counts, "errors": errors}

    # ── discovery ───────────────────────────────────────────────────────────────────

    async def _discover(self, stack: AsyncExitStack, now: datetime, errors: list[str]):
        sessions: dict[str, asyncssh.SFTPClient] = {}
        index: dict[str, list[tuple[Endpoint, RemoteFile]]] = {}

        async def one(ep: Endpoint) -> None:
            t0 = _time.perf_counter()
            try:
                sftp = await stack.enter_async_context(self._session(ep))
                files = await list_dir(sftp, ep.path)
            except (TransferError, asyncssh.Error, OSError) as exc:
                text = exc.text if isinstance(exc, TransferError) else str(exc)
                status = "auth_failed" if "permission denied" in text.lower() else "unreachable"
                db.set_health(self.con, ep.name, "staging", status, text[:200], None, now)
                errors.append(f"{ep.name}: {text[:120]}")
                return
            ms = int((_time.perf_counter() - t0) * 1000)
            sessions[ep.name] = sftp
            db.set_health(self.con, ep.name, "staging", "ok", f"{len(files)} files listed", ms, now)
            for f in files:
                index.setdefault(f.name, []).append((ep, f))

        await asyncio.gather(*(one(ep) for ep in self.topo.staging))
        for hits in index.values():
            hits.sort(key=lambda h: h[0].name)
        return sessions, index

    async def _probe_destinations(self, todo: list[Branch], day: date, now: datetime) -> None:
        """Log in to each open branch's destination so health shows problems before a file is ready.
        Endpoints that refused our login are not probed again (repeated bad logins can lock accounts)."""
        health = db.health(self.con)

        async def one(b: Branch) -> None:
            ep = self.topo.destinations[b.destination]
            h = health.get(ep.name)
            row = db.get(self.con, day, b.branch_id)
            if h is not None and h["status"] == "auth_failed" and not row["retry_authorized"]:
                return
            if h is not None and h["status"] == "ok" and h["checked_at"] == db.ts(now):
                return
            t0 = _time.perf_counter()
            try:
                async with self._session(ep) as sftp:
                    await sftp.stat(ep.path)
            except TransferError as exc:
                status = "auth_failed" if "permission denied" in exc.text.lower() else "unreachable"
                db.set_health(self.con, ep.name, "destination", status, exc.text[:200], None, now)
                return
            except (asyncssh.Error, OSError) as exc:
                db.set_health(self.con, ep.name, "destination", "unreachable", str(exc)[:200], None, now)
                return
            prev = h["status"] if h is not None else None
            if prev in (None, "unreachable", "auth_failed"):
                db.set_health(self.con, ep.name, "destination", "ok", "login ok",
                              int((_time.perf_counter() - t0) * 1000), now)

        await asyncio.gather(*(one(b) for b in todo))

    # ── per branch ──────────────────────────────────────────────────────────────────

    def _fail(self, b: Branch, day: date, now: datetime, run_id: int, stage: str, detail: str,
              gate: bool = False, counts: dict | None = None, log: bool = True, **extra) -> None:
        row = db.get(self.con, day, b.branch_id)
        same = row["status"] == "failed" and row["failure_stage"] == stage and row["failure_detail"] == detail
        db.update(self.con, day, b.branch_id, now, status="failed", failure_stage=stage, failure_detail=detail,
                  failure_since=row["failure_since"] if same else db.ts(now), human_gate=int(gate),
                  retry_authorized=0, **extra)
        if not same and not log and counts is not None:
            counts["failed"] += 1
        if not same and log:
            db.event(self.con, now, day, b.branch_id, stage, "fail", detail, run_id, extra.get("file_name"))
            if counts is not None:
                counts["failed"] += 1

    async def _branch(self, b: Branch, day: date, now: datetime, run_id: int, sessions, index, counts) -> None:
        con = self.con
        row = db.get(con, day, b.branch_id)
        name, ctl_name = ef.csv_name(b.branch_id, day), ef.ctl_name(b.branch_id, day)
        deadline = self.deadline(day)
        expected = datetime.combine(day, time(*(int(x) for x in b.expected_by.split(":"))))
        hits = [(ep, f) for ep, f in index.get(name, []) if ep.name in sessions]
        unreachable = [ep.name for ep in self.topo.staging if ep.name not in sessions]
        searched = f"searched {len(sessions)} of {len(self.topo.staging)} staging servers"
        if unreachable:
            searched += f" ({', '.join(unreachable)} unreachable)"

        if not hits:
            if now >= deadline:
                self._fail(b, day, now, run_id, "discover",
                           f"{name} not found by the deadline {deadline:%H:%M}; {searched}", counts=counts)
            elif now >= expected:
                if row["status"] != "late":
                    db.update(con, day, b.branch_id, now, status="late", failure_stage="discover",
                              failure_detail=f"{name} not found; expected by {b.expected_by}; {searched}")
                    db.event(con, now, day, b.branch_id, "discover", "late",
                             f"not found; expected by {b.expected_by}; {searched}", run_id, name)
            return

        # Read every copy (normally one). Identical copies are fine; different copies need a person.
        copies = []
        for ep, f in hits:
            data = await read_file(sessions[ep.name], posixpath.join(ep.path, name))
            ctl_hit = [x for x in index.get(ctl_name, []) if x[0].name == ep.name]
            ctl = await read_file(sessions[ep.name], posixpath.join(ep.path, ctl_name)) if ctl_hit else None
            copies.append((ep, f, data, ctl))
        digests = {ef.sha256(c[2]) for c in copies}
        if len(digests) > 1:
            parts = [f"{ep.name} ({len(d)} bytes, sha256 {ef.sha256(d)[:10]}…)" for ep, _, d, _ in copies]
            self._fail(b, day, now, run_id, "discover", "different contents under the same name: " + " and ".join(parts),
                       counts=counts, file_name=name)
            return
        ep, f, data, ctl = copies[0]
        sig = "|".join(f"{c[0].name}:{len(c[2])}:{ef.sha256(c[2])[:16]}:{bool(c[3])}" for c in copies)
        if row["seen_sig"] != sig:
            mtime = datetime.fromtimestamp(f.mtime).strftime("%H:%M") if f.mtime else "?"
            db.event(con, now, day, b.branch_id, "discover", "ok",
                     f"found on {ep.name}: {len(data):,} bytes, modified {mtime}" + ("" if ctl else "; no control file yet"),
                     run_id, name)
            if len(copies) > 1:
                others = ", ".join(c[0].name for c in copies[1:])
                db.event(con, now, day, b.branch_id, "discover", "info",
                         f"identical copy also on {others} (same sha256); using {ep.name}, the copy is ignored",
                         run_id, name)
            if row["status"] in ("waiting", "late"):
                counts["found"] += 1
        db.update(con, day, b.branch_id, now, seen_sig=sig, source=ep.name, file_name=name)

        check = ef.validate(data, ctl, day)
        if not check.ok:
            if check.code == "no_control" and now < deadline:
                if row["note"] != "waiting for control file":
                    db.event(con, now, day, b.branch_id, "validate", "info",
                             "CSV is here but its control file is not; checking again next pass", run_id, name)
                db.update(con, day, b.branch_id, now, status="found", size=check.size, rows=check.rows,
                          sha256=check.sha256, found_at=row["found_at"] or db.ts(now), note="waiting for control file",
                          failure_stage=None, failure_detail=None)
                return
            if check.code == "no_control":
                check.detail = f"control file (.ctl) not present by the deadline {deadline:%H:%M}"
            self._fail(b, day, now, run_id, "validate", check.detail, counts=counts, size=check.size,
                       rows=check.rows, sha256=check.sha256)
            return
        if row["sha256"] != check.sha256 or row["status"] in ("waiting", "late", "failed") or row["note"]:
            db.event(con, now, day, b.branch_id, "validate", "ok", check.detail, run_id, name)
        db.update(con, day, b.branch_id, now, status="found", size=check.size, rows=check.rows, sha256=check.sha256,
                  found_at=row["found_at"] or db.ts(now), note=None, failure_stage=None, failure_detail=None,
                  human_gate=0)
        row = db.get(con, day, b.branch_id)

        if row["sent_sha"] != check.sha256:
            ok = await self._transfer(b, day, now, run_id, name, data, ctl_name, ctl, check, counts)
            if not ok:
                return
        await self._schedule(b, day, now, run_id, name, counts)

    async def _watch_delivered(self, b: Branch, day: date, now: datetime, run_id: int, sessions, index,
                               counts) -> None:
        """After delivery, a different file under the same name (a re-export, a second copy) needs a person:
        the destination may now hold the wrong version. The engine never re-delivers on its own."""
        row = db.get(self.con, day, b.branch_id)
        name = ef.csv_name(b.branch_id, day)
        for ep, f in index.get(name, []):
            if ep.name not in sessions:
                continue
            if ep.name == row["source"] and f.size == row["size"]:
                continue  # the delivered copy itself; a size check keeps the watch cheap
            data = await read_file(sessions[ep.name], posixpath.join(ep.path, name))
            digest = ef.sha256(data)
            if digest == row["sent_sha"]:
                continue
            self._fail(b, day, now, run_id, "discover",
                       f"different contents appeared after delivery: {ep.name} has {len(data):,} bytes "
                       f"(sha256 {digest[:10]}…); the delivered copy from {row['source']} is sha256 "
                       f"{(row['sent_sha'] or '')[:10]}…. Job #{self._job_id(day, b.branch_id)} is still queued",
                       gate=True, counts=counts, file_name=name)
            return

    def _job_id(self, day: date, branch_id: str) -> str:
        job = self.con.execute("SELECT id FROM jobs WHERE business_date=? AND branch_id=?",
                               (day.isoformat(), branch_id)).fetchone()
        return str(job["id"]) if job else "?"

    async def _transfer(self, b: Branch, day: date, now: datetime, run_id: int, name: str, data: bytes,
                        ctl_name: str, ctl: bytes, check: ef.Check, counts: dict) -> bool:
        con = self.con
        dest = self.topo.destinations[b.destination]
        remote = posixpath.join(dest.path, name)
        authorised = bool(db.get(con, day, b.branch_id)["retry_authorized"])
        attempts = self.settings.upload_attempts
        last: TransferError | None = None
        for attempt in range(1, attempts + 1):
            stage = "connect"
            try:
                async with self._session(dest) as sftp:
                    stage = "upload"
                    for fname, payload in ((name, data), (ctl_name, ctl)):
                        target = posixpath.join(dest.path, fname)
                        if await exists(sftp, target) and ef.sha256(await read_file(sftp, target)) == ef.sha256(payload):
                            db.event(con, now, day, b.branch_id, "transfer", "info",
                                     f"{fname} already at {dest.name} with the same sha256; not sent again", run_id, name)
                            out = {"bytes": len(payload), "sha256": ef.sha256(payload)}
                        else:
                            out = await deliver(sftp, dest.path, fname, payload)
                        if fname == name:
                            result = out
                    break
            except TransferError as exc:
                last = exc
            except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
                last = TransferError(stage, exc)
            gate = self._gate_reason(last)
            db.event(con, now, day, b.branch_id, last.stage, "fail",
                     f"attempt {attempt} of {attempts} to {dest.name}: {last.kind}: {last.text}", run_id, name)
            db.set_health(con, dest.name, "destination", gate or "unreachable", f"{last.kind}: {last.text}"[:200],
                          None, now)
            if gate:
                self._fail(b, day, now, run_id, last.stage, f"{last.kind}: {last.text} ({dest.name})", gate=True,
                           counts=counts, attempts=attempt, log=False)
                db.event(con, now, day, b.branch_id, "transfer", "info",
                         "stopped: this error needs a person before another attempt", run_id, name)
                if authorised:
                    db.event(con, now, day, b.branch_id, "transfer", "info",
                             "the retry a person authorised failed; waiting for a person again", run_id, name)
                return False
            if attempt < attempts:
                await asyncio.sleep(self.settings.retry_backoff_s * attempt)
        else:
            self._fail(b, day, now, run_id, last.stage,
                       f"{last.kind}: {last.text} ({dest.name}); {attempts} attempts this pass, will retry next pass",
                       counts=counts, attempts=attempts)
            return False
        db.set_health(con, dest.name, "destination", "ok", "delivered", None, now)
        db.event(con, now, day, b.branch_id, "transfer", "ok",
                 f"delivered to {dest.name}:{remote}, {result['bytes']:,} bytes, attempt {attempt} of {attempts}",
                 run_id, name)
        db.event(con, now, day, b.branch_id, "verify", "ok",
                 f"read back {result['bytes']:,} bytes; sha256 {result['sha256'][:12]}… matches the source", run_id, name)
        db.update(con, day, b.branch_id, now, status="sent", sent_sha=check.sha256, remote_path=f"{dest.name}:{remote}",
                  sent_at=db.ts(now), failure_stage=None, failure_detail=None, human_gate=0, retry_authorized=0,
                  attempts=attempt)
        counts["sent"] += 1
        return True

    @staticmethod
    def _gate_reason(err: TransferError) -> str | None:
        """Errors a person must look at before the engine tries again. Decided by error type, not by the LLM."""
        text = err.text.lower()
        if err.stage == "connect" and err.kind == "PermissionDenied":
            return "auth_failed"
        if err.kind in ("SFTPNoSpaceOnFilesystem", "SFTPQuotaExceeded") or "no space left" in text:
            return "disk_full"
        if err.kind == "SFTPPermissionDenied":
            return "no_write"
        return None

    async def _schedule(self, b: Branch, day: date, now: datetime, run_id: int, name: str, counts: dict) -> None:
        row = db.get(self.con, day, b.branch_id)
        job, created = scheduler.enqueue(self.con, day, b.branch_id, name, b.destination, row["remote_path"] or "",
                                         b.schedule_slot, now)
        if created:
            db.event(self.con, now, day, b.branch_id, "schedule", "ok",
                     f"queued job #{job['id']} for {job['run_at'][11:16]} at {b.destination}", run_id, name)
        else:
            db.event(self.con, now, day, b.branch_id, "schedule", "info",
                     f"job #{job['id']} already queued; not queued again", run_id, name)
        db.update(self.con, day, b.branch_id, now, status="scheduled", scheduled_at=db.ts(now))
        counts["scheduled"] += 1
