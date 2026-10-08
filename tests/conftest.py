import json
from datetime import date, datetime, time
from pathlib import Path

import asyncssh
import pytest

from handoff import db
from handoff import exportfile as ef
from handoff import generator as gen
from handoff.engine import Engine
from handoff.settings import Settings
from handoff.sftp_farm import SftpFarm
from handoff.topology import default_topology

DAY = date(2026, 10, 8)


def at(hh: int, mm: int) -> datetime:
    return datetime.combine(DAY, time(hh, mm))


class Env:
    """Real SFTP servers on ephemeral ports + an engine pointed at them."""

    def __init__(self, tmp: Path, behaviours=None, wrong_password=()):
        self.tmp = tmp
        self.topo0 = default_topology(0, 0)
        self.settings = Settings(var=tmp, db=tmp / "t.db", credentials=tmp / "creds.json",
                                 known_hosts=tmp / "known_hosts", retry_backoff_s=0.01)
        self.roots = tmp / "roots"
        self.passwords = {ep.name: f"pw-{ep.name}" for ep in self.topo0.all_endpoints()}
        self.client_pw = dict(self.passwords)
        for name in wrong_password:
            self.client_pw[name] = "stale-password"
        self.key = asyncssh.generate_private_key("ssh-ed25519")
        self.farm = SftpFarm(self.topo0, self.roots, self.passwords, self.key, behaviours or {})

    async def start(self):
        await self.farm.start()
        self.topo = self.farm.topology()
        self.farm.write_known_hosts(self.settings.known_hosts, self.topo)
        self.con = db.connect(self.settings.db)
        self.engine = Engine(self.con, self.topo, self.settings, self.client_pw)
        return self

    def put(self, endpoint: str, name: str, data: bytes, when: datetime | None = None):
        gen.apply_drops(self.roots, self.topo0, [gen.Drop(when or at(5, 30), endpoint, name, data)])

    def put_pair(self, branch_id: str, rows: int = 20, endpoint: str | None = None, data: bytes | None = None,
                 ctl: bytes | None = None):
        import random
        d, c = gen.make_pair(random.Random(branch_id), branch_id, DAY, rows, at(5, 30))
        ep = endpoint or self.topo0.branch(branch_id).home_staging
        self.put(ep, ef.csv_name(branch_id, DAY), d if data is None else data)
        self.put(ep, ef.ctl_name(branch_id, DAY), c if ctl is None else ctl)
        return d, c

    def dest_file(self, branch_id: str) -> Path:
        b = self.topo0.branch(branch_id)
        return self.roots / b.destination / "inbound" / ef.csv_name(branch_id, DAY)

    def row(self, branch_id: str):
        return db.get(self.con, DAY, branch_id)

    def events(self, branch_id: str):
        return [(e["stage"], e["outcome"]) for e in db.events(self.con, DAY, branch_id)]

    async def stop(self):
        await self.farm.stop()


@pytest.fixture
async def env(tmp_path):
    e = await Env(tmp_path).start()
    yield e
    await e.stop()


@pytest.fixture
async def make_env(tmp_path):
    made = []

    async def make(**kw):
        e = await Env(tmp_path, **kw).start()
        made.append(e)
        return e

    yield make
    for e in made:
        await e.stop()
