"""Synthetic branch export files and the failures injected into a simulated morning.

Everything here is invented: branch names, item codes, amounts. A scenario is a list of
timed file drops onto staging servers plus endpoint behaviours (disk full, permission
denied, a dropped connection, a wrong password, a staging server that starts late).
"""

from __future__ import annotations

import json
import random
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import exportfile as ef
from .topology import Topology

DOC_TYPES = ["SALE"] * 8 + ["RETURN", "ADJUST", "TRANSFER"]

# Failure labels the simulator can inject, and the category a correct classifier should give.
INJECTABLE = {
    "missing": "missing",
    "late": "late",
    "empty": "empty",
    "truncated": "truncated",
    "checksum_mismatch": "checksum_mismatch",
    "duplicate_conflict": "duplicate_conflict",
    "stale": "stale",
    "auth_failure": "auth_failure",
    "disk_full": "disk_full",
    "permission_denied": "permission_denied",
    "transient": "network",
}


@dataclass
class Drop:
    at: datetime
    endpoint: str
    name: str
    data: bytes
    note: str = ""


@dataclass
class Scenario:
    day: date
    drops: list[Drop] = field(default_factory=list)
    behaviours: dict[str, set[str]] = field(default_factory=dict)  # destination -> {"disk_full", ...}
    offline: dict[str, tuple[datetime, datetime]] = field(default_factory=dict)  # staging -> window
    wrong_password: set[str] = field(default_factory=set)  # endpoints where our stored password is stale
    injected: dict[str, str] = field(default_factory=dict)  # branch -> injected label
    notes: dict[str, str] = field(default_factory=dict)  # branch -> what happens, for the README/results

    def drops_due(self, since: datetime | None, until: datetime) -> list[Drop]:
        return [d for d in self.drops if (since is None or d.at > since) and d.at <= until]


def _rows(rng: random.Random, branch_id: str, day: date, n: int) -> list[list[str]]:
    out = []
    for i in range(1, n + 1):
        qty = rng.randint(1, 12)
        price = rng.choice([1.25, 2.5, 3.75, 4.2, 6.9, 9.99, 12.5, 18.0, 24.75])
        out.append([f"{branch_id}-{i:06d}", branch_id, day.isoformat(), rng.choice(DOC_TYPES),
                    f"IT{rng.randint(10000, 99999)}", str(qty), f"{qty * price:.2f}"])
    return out


def make_pair(rng: random.Random, branch_id: str, day: date, n_rows: int, created: datetime,
              row_date: date | None = None) -> tuple[bytes, bytes]:
    rows = _rows(rng, branch_id, row_date or day, n_rows)
    data = ef.render_csv(rows)
    ctl = ef.render_ctl(ef.csv_name(branch_id, day), day, n_rows, ef.sha256(data), created.isoformat())
    return data, ctl


def _at(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm, ss))


def _add_pair(sc: Scenario, at: datetime, endpoint: str, branch_id: str, data: bytes, ctl: bytes,
              ctl_delay_min: int = 0, note: str = "") -> None:
    sc.drops.append(Drop(at, endpoint, ef.csv_name(branch_id, sc.day), data, note))
    sc.drops.append(Drop(at + timedelta(minutes=ctl_delay_min, seconds=20), endpoint,
                         ef.ctl_name(branch_id, sc.day), ctl, note))


def _yesterday_noise(sc: Scenario, topo: Topology, rng: random.Random) -> None:
    """Yesterday's files are still sitting on staging; the engine must ignore them."""
    prev = sc.day - timedelta(days=1)
    for b in topo.branches:
        data, ctl = make_pair(rng, b.branch_id, prev, rng.randint(40, 90), _at(prev, 5, 30))
        at = _at(sc.day, 0, 0)
        sc.drops.append(Drop(at, b.home_staging, ef.csv_name(b.branch_id, prev), data, "yesterday"))
        sc.drops.append(Drop(at, b.home_staging, ef.ctl_name(b.branch_id, prev), ctl, "yesterday"))


def demo_scenario(day: date, topo: Topology, seed: int = 8) -> Scenario:
    """A deliberately failure-heavy morning: every failure type appears at least once."""
    rng = random.Random(seed)
    sc = Scenario(day)
    _yesterday_noise(sc, topo, rng)
    staging_names = [s.name for s in topo.staging]
    special = {
        "BR03": "missing", "BR05": "late", "BR08": "empty", "BR09": "transient", "BR11": "truncated",
        "BR13": "checksum_mismatch", "BR17": "duplicate_conflict", "BR19": "stale",
        "BR20": "auth_failure", "BR22": "disk_full", "BR23": "permission_denied",
    }
    late_morning = {"BR02": (6, 3), "BR06": (6, 8), "BR12": (6, 12), "BR21": (6, 4), "BR24": (6, 17)}
    for b in topo.branches:
        bid, home = b.branch_id, b.home_staging
        n = rng.randint(300, 1400)
        hh, mm = late_morning.get(bid, (5, rng.randint(20, 58)))
        at = _at(day, hh, mm, rng.randint(0, 59))
        label = special.get(bid)
        if label:
            sc.injected[bid] = label
        if label == "missing":
            sc.notes[bid] = "No file arrives all morning."
            continue
        if label == "late":
            at = _at(day, 7, 2, 10)
            sc.notes[bid] = "File arrives at 07:02, after the 06:xx expected time."
        data, ctl = make_pair(rng, bid, day, n, at)
        if label == "empty":
            data = b""
            ctl = ef.render_ctl(ef.csv_name(bid, day), day, 0, ef.sha256(b""), at.isoformat())
            sc.notes[bid] = "0-byte CSV with a control file."
        elif label == "truncated":
            data = data[: int(len(data) * 0.62)]
            sc.notes[bid] = "Upload stopped at 62% of the file."
        elif label == "checksum_mismatch":
            good = data
            lines = data.decode().split("\n")
            parts = lines[7].split(",")
            parts[6] = f"{float(parts[6]) + 10:.2f}"
            lines[7] = ",".join(parts)
            data = "\n".join(lines).encode()
            fixed_at = _at(day, 7, 8, 30)
            _add_pair(sc, fixed_at, home, bid, good, ctl, note="corrected re-send")
            sc.notes[bid] = "One amount changed after the control file was written; a corrected file is re-sent at 07:08."
        elif label == "stale":
            data, ctl = make_pair(rng, bid, day, n, at, row_date=day - timedelta(days=1))
            sc.notes[bid] = "Today's file name, but the rows are dated yesterday."
        elif label == "duplicate_conflict":
            other = staging_names[(staging_names.index(home) + 4) % len(staging_names)]
            data2, ctl2 = make_pair(rng, bid, day, n + 37, at + timedelta(minutes=9))
            _add_pair(sc, at + timedelta(minutes=9), other, bid, data2, ctl2, note="second, different copy")
            sc.notes[bid] = f"Two different versions: {home} and {other}."
        elif label == "auth_failure":
            sc.wrong_password.add(b.destination)
            sc.notes[bid] = "The stored password for the destination is out of date."
        elif label == "disk_full":
            sc.behaviours.setdefault(b.destination, set()).add("disk_full")
            sc.notes[bid] = "Destination reports no space left on device."
        elif label == "permission_denied":
            sc.behaviours.setdefault(b.destination, set()).add("permission_denied")
            sc.notes[bid] = "Login works, but the inbound folder is read-only."
        elif label == "transient":
            sc.behaviours.setdefault(b.destination, set()).add("transient_once")
            sc.notes[bid] = "Destination drops the connection on the first upload; the retry succeeds."
        ctl_delay = 3 if bid == "BR21" else 0
        if bid == "BR21":
            sc.notes[bid] = "Control file lands 3 minutes after the CSV."
        _add_pair(sc, at, home, bid, data, ctl, ctl_delay_min=ctl_delay)
    # Extra situations that are not failures of a branch:
    b16 = topo.branch("BR16")
    other16 = staging_names[(staging_names.index(b16.home_staging) + 3) % len(staging_names)]
    dup = [d for d in sc.drops if d.name.startswith("BR16_") and d.note == ""]
    for d in dup:
        sc.drops.append(Drop(d.at + timedelta(minutes=2), other16, d.name, d.data, "identical copy"))
    sc.notes["BR16"] = f"Identical copy on {b16.home_staging} and {other16}: delivered once."
    b14 = topo.branch("BR14")
    sc.offline[b14.home_staging] = (_at(day, 0, 0), _at(day, 6, 15))
    sc.notes["BR14"] = f"{b14.home_staging} is unreachable until 06:15."
    sc.drops.sort(key=lambda d: d.at)
    return sc


def random_scenario(day: date, topo: Topology, seed: int, max_failures: int = 5) -> Scenario:
    """A random morning for batch measurement: 1 to max_failures injected failures."""
    rng = random.Random(seed)
    sc = Scenario(day)
    labels = list(INJECTABLE)
    chosen = rng.sample(topo.branches, rng.randint(1, max_failures))
    special = {b.branch_id: rng.choice(labels) for b in chosen}
    staging_names = [s.name for s in topo.staging]
    for b in topo.branches:
        bid = b.branch_id
        home = rng.choice(staging_names) if rng.random() < 0.2 else b.home_staging
        at = _at(day, 5, rng.randint(10, 59), rng.randint(0, 59)) if rng.random() < 0.8 else \
            _at(day, 6, rng.randint(0, 15), rng.randint(0, 59))
        label = special.get(bid)
        if label:
            sc.injected[bid] = label
        if label == "missing":
            continue
        if label == "late":
            at = _at(day, 6, 45) + timedelta(minutes=rng.randint(0, 35), seconds=rng.randint(0, 59))
        n = rng.randint(50, 600)
        data, ctl = make_pair(rng, bid, day, n, at)
        if label == "empty":
            data = b""
            ctl = ef.render_ctl(ef.csv_name(bid, day), day, 0, ef.sha256(b""), at.isoformat())
        elif label == "truncated":
            data = data[: int(len(data) * rng.uniform(0.2, 0.95))]
            if data.endswith(b"\n"):
                data = data[:-5]
        elif label == "checksum_mismatch":
            lines = data.decode().split("\n")
            k = rng.randint(1, n)
            parts = lines[k].split(",")
            parts[5] = str(int(parts[5]) + 1)
            lines[k] = ",".join(parts)
            data = "\n".join(lines).encode()
        elif label == "stale":
            data, ctl = make_pair(rng, bid, day, n, at, row_date=day - timedelta(days=rng.choice([1, 2, 3])))
        elif label == "duplicate_conflict":
            other = rng.choice([s for s in staging_names if s != home])
            d2, c2 = make_pair(rng, bid, day, n + rng.randint(1, 40), at + timedelta(minutes=3))
            _add_pair(sc, at + timedelta(minutes=3), other, bid, d2, c2)
        elif label == "auth_failure":
            sc.wrong_password.add(b.destination)
        elif label in ("disk_full", "permission_denied"):
            sc.behaviours.setdefault(b.destination, set()).add(label)
        elif label == "transient":
            sc.behaviours.setdefault(b.destination, set()).add("transient_once")
        _add_pair(sc, at, home, bid, data, ctl)
    sc.drops.sort(key=lambda d: d.at)
    return sc


# ── environment on disk ────────────────────────────────────────────────────────────

def make_credentials(topo: Topology, sc: Scenario) -> tuple[dict[str, str], dict[str, str]]:
    """(server-side truth, what the handoff engine has stored)."""
    server = {ep.name: secrets.token_urlsafe(12) for ep in topo.all_endpoints()}
    client = dict(server)
    for name in sc.wrong_password:
        client[name] = secrets.token_urlsafe(12)
    return server, client


def prepare_dirs(sim_dir: Path, topo: Topology) -> Path:
    roots = sim_dir / "roots"
    for ep in topo.all_endpoints():
        (roots / ep.name / ep.path.strip("/")).mkdir(parents=True, exist_ok=True)
    return roots


def apply_drops(roots: Path, topo: Topology, drops: list[Drop]) -> None:
    import os
    for d in drops:
        ep = topo.endpoint(d.endpoint)
        target = roots / ep.name / ep.path.strip("/") / d.name
        tmp = target.with_name("." + target.name + ".tmp")
        tmp.write_bytes(d.data)
        os.replace(tmp, target)
        ts = d.at.timestamp()
        os.utime(target, (ts, ts))


def scenario_summary(sc: Scenario) -> dict:
    return {"day": sc.day.isoformat(), "injected": sc.injected, "notes": sc.notes,
            "offline": {k: [a.isoformat(), b.isoformat()] for k, (a, b) in sc.offline.items()},
            "behaviours": {k: sorted(v) for k, v in sc.behaviours.items()},
            "wrong_password": sorted(sc.wrong_password),
            "drops": len(sc.drops)}


def write_summary(path: Path, sc: Scenario) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(scenario_summary(sc), indent=2) + "\n")
