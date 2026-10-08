"""Who sends what where: staging servers, destination endpoints and branches.

The topology files hold no secrets. Passwords live in a separate credentials file
(the simulator writes one to var/sim/, git-ignored) or in environment variables
named HANDOFF_PASSWORD_<ENDPOINT> (upper case, dashes become underscores).
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path

N_STAGING = 9
N_BRANCHES = 24

BRANCH_NAMES = [
    "Mill Road", "Harbour Street", "Station Square", "Orchard Lane", "Quarry Hill", "Canal Walk",
    "Market Cross", "Linden Park", "Foundry Yard", "Beacon Rise", "Willow Bank", "Granary Row",
    "Copper Gate", "Ferry Point", "Chapel Court", "Kiln Lane", "Salt Meadow", "Tannery Close",
    "Lantern Hill", "Ropewalk", "Weaver Street", "Pinfold Green", "Highfield", "Brook Meadow",
]


@dataclass(frozen=True)
class Endpoint:
    name: str
    kind: str  # "staging" or "destination"
    host: str
    port: int
    username: str
    path: str  # folder to read from (staging) or deliver into (destination)


@dataclass(frozen=True)
class Branch:
    branch_id: str
    name: str
    destination: str
    home_staging: str
    expected_by: str  # "HH:MM": after this the file counts as late
    schedule_slot: str  # "HH:MM": when the destination job should run


@dataclass
class Topology:
    staging: list[Endpoint]
    destinations: dict[str, Endpoint]
    branches: list[Branch]

    def endpoint(self, name: str) -> Endpoint:
        for ep in self.staging:
            if ep.name == name:
                return ep
        return self.destinations[name]

    def all_endpoints(self) -> list[Endpoint]:
        return [*self.staging, *self.destinations.values()]

    def with_ports(self, ports: dict[str, int]) -> "Topology":
        """Copy with real ports (tests bind to port 0 and learn the port afterwards)."""
        return Topology(
            staging=[replace(e, port=ports.get(e.name, e.port)) for e in self.staging],
            destinations={k: replace(e, port=ports.get(k, e.port)) for k, e in self.destinations.items()},
            branches=list(self.branches),
        )

    def branch(self, branch_id: str) -> Branch:
        for b in self.branches:
            if b.branch_id == branch_id:
                return b
        raise KeyError(branch_id)


def default_topology(staging_base: int = 2201, dest_base: int = 2301, host: str = "127.0.0.1") -> Topology:
    staging = [Endpoint(f"staging-{i:02d}", "staging", host, staging_base + i - 1 if staging_base else 0,
                        "collector", "/exports") for i in range(1, N_STAGING + 1)]
    branches, dests = [], {}
    for i in range(1, N_BRANCHES + 1):
        bid = f"BR{i:02d}"
        dest = f"dest-{bid.lower()}"
        dests[dest] = Endpoint(dest, "destination", host, dest_base + i - 1 if dest_base else 0,
                               f"{bid.lower()}-drop", "/inbound")
        expected = f"06:{20 + (i * 7) % 21:02d}"  # spread between 06:20 and 06:40
        slot_min = 8 * 60 + (i - 1) * 2
        branches.append(Branch(bid, BRANCH_NAMES[i - 1], dest, staging[(i - 1) % N_STAGING].name, expected,
                               f"{slot_min // 60:02d}:{slot_min % 60:02d}"))
    return Topology(staging, dests, branches)


def save_topology(topo: Topology, topology_path: Path, branches_path: Path) -> None:
    topology_path.parent.mkdir(parents=True, exist_ok=True)
    data = {"staging": [asdict(e) for e in topo.staging],
            "destinations": [asdict(e) for e in topo.destinations.values()]}
    topology_path.write_text(json.dumps(data, indent=2) + "\n")
    with branches_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(topo.branches[0]).keys()))
        w.writeheader()
        for b in topo.branches:
            w.writerow(asdict(b))


def load_topology(topology_path: Path, branches_path: Path) -> Topology:
    data = json.loads(Path(topology_path).read_text())
    staging = [Endpoint(**e) for e in data["staging"]]
    dests = {e["name"]: Endpoint(**e) for e in data["destinations"]}
    with Path(branches_path).open(newline="") as f:
        branches = [Branch(**row) for row in csv.DictReader(f)]
    for b in branches:
        if b.destination not in dests:
            raise ValueError(f"{b.branch_id}: unknown destination {b.destination}")
    return Topology(staging, dests, branches)


def load_credentials(path: Path | None) -> dict[str, str]:
    """Endpoint name -> password. Environment variables override the file."""
    creds: dict[str, str] = {}
    if path and Path(path).exists():
        creds.update(json.loads(Path(path).read_text()))
    for key, value in os.environ.items():
        if key.startswith("HANDOFF_PASSWORD_"):
            creds[key[len("HANDOFF_PASSWORD_"):].lower().replace("_", "-")] = value
    return creds
