"""A farm of real SFTP servers on localhost, for the simulator and the tests.

Each staging server and each destination endpoint is its own asyncssh listener with
its own TCP port, its own root folder (chroot) and its own password. They run inside
one Python process and share one host key. The engine talks to them over real SSH/SFTP,
exactly as it would to remote machines.

Destination behaviours can be switched on per endpoint:
- "disk_full": every write fails with "No space left on device"
- "permission_denied": opening a file for writing fails with "Permission denied"
- "transient_once": the first upload to the endpoint drops the connection (once)
"""

from __future__ import annotations

import asyncio
import hmac
import os
from collections import defaultdict
from pathlib import Path

import asyncssh

from .topology import Endpoint, Topology

WRITE_V3 = asyncssh.FXF_WRITE | asyncssh.FXF_CREAT | asyncssh.FXF_TRUNC | asyncssh.FXF_APPEND
WRITE_V56 = asyncssh.ACE4_WRITE_DATA | asyncssh.ACE4_APPEND_DATA


class _Server(asyncssh.SSHServer):
    def __init__(self, farm: "SftpFarm", ep: Endpoint) -> None:
        self.farm, self.ep = farm, ep

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        self.farm.stats[self.ep.name]["connections"] += 1

    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        ok = username == self.ep.username and hmac.compare_digest(
            password.encode(), self.farm.passwords.get(self.ep.name, "").encode())
        if not ok:
            self.farm.stats[self.ep.name]["auth_failures"] += 1
        return ok


class _Sftp(asyncssh.SFTPServer):
    def __init__(self, chan: asyncssh.SSHServerChannel, farm: "SftpFarm", ep: Endpoint) -> None:
        super().__init__(chan, chroot=str(farm.roots / ep.name).encode())
        self.farm, self.ep = farm, ep

    def _before_write_open(self, path: bytes) -> None:
        behaviour = self.farm.behaviours.get(self.ep.name, set())
        if "permission_denied" in behaviour:
            raise asyncssh.SFTPPermissionDenied("Permission denied")
        if "transient_once" in behaviour and not self.farm.dropped[self.ep.name]:
            self.farm.dropped[self.ep.name].add(path)
            self.connection.abort()
            raise asyncssh.SFTPConnectionLost("Connection reset by peer")
        self.farm.stats[self.ep.name]["write_opens"] += 1

    def open(self, path, pflags, attrs):
        if pflags & WRITE_V3:
            self._before_write_open(path)
        return super().open(path, pflags, attrs)

    def open56(self, path, desired_access, flags, attrs):
        if desired_access & WRITE_V56:
            self._before_write_open(path)
        return super().open56(path, desired_access, flags, attrs)

    def write(self, file_obj, offset, data):
        if "disk_full" in self.farm.behaviours.get(self.ep.name, set()):
            raise asyncssh.SFTPNoSpaceOnFilesystem("No space left on device")
        return super().write(file_obj, offset, data)

    def _count_delivery(self, newpath: bytes) -> None:
        if not newpath.endswith(b".part"):
            self.farm.stats[self.ep.name]["deliveries"] += 1

    def rename(self, oldpath, newpath):
        self._count_delivery(newpath)
        return super().rename(oldpath, newpath)

    def posix_rename(self, oldpath, newpath):
        self._count_delivery(newpath)
        return super().posix_rename(oldpath, newpath)


class SftpFarm:
    def __init__(self, topo: Topology, roots: Path, passwords: dict[str, str],
                 host_key: asyncssh.SSHKey, behaviours: dict[str, set[str]] | None = None) -> None:
        self.topo, self.roots, self.passwords, self.host_key = topo, Path(roots), passwords, host_key
        self.behaviours = {k: set(v) for k, v in (behaviours or {}).items()}
        self.dropped: dict[str, set[bytes]] = defaultdict(set)
        self.stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.acceptors: dict[str, asyncssh.SSHAcceptor] = {}
        self.ports: dict[str, int] = {}

    async def start(self, skip: set[str] | None = None) -> "SftpFarm":
        for ep in self.topo.all_endpoints():
            (self.roots / ep.name / ep.path.strip("/")).mkdir(parents=True, exist_ok=True)
            if skip and ep.name in skip:
                continue
            await self.start_endpoint(ep.name)
        return self

    async def start_endpoint(self, name: str) -> int:
        if name in self.acceptors:
            return self.ports[name]
        ep = self.topo.endpoint(name)
        port = self.ports.get(name, ep.port)
        acceptor = await asyncssh.listen(
            ep.host, port, server_host_keys=[self.host_key],
            server_factory=lambda ep=ep: _Server(self, ep),
            sftp_factory=lambda chan, ep=ep: _Sftp(chan, self, ep),
            allow_scp=False, process_factory=None, keepalive_interval=0)
        self.acceptors[name] = acceptor
        self.ports[name] = acceptor.get_port()
        return self.ports[name]

    async def stop_endpoint(self, name: str) -> None:
        acc = self.acceptors.pop(name, None)
        if acc:
            acc.close()
            await acc.wait_closed()

    async def stop(self) -> None:
        for name in list(self.acceptors):
            await self.stop_endpoint(name)

    def topology(self) -> Topology:
        """The topology with the ports actually bound (needed when ports were 0)."""
        return self.topo.with_ports(self.ports)

    def write_known_hosts(self, path: Path, topo: Topology | None = None) -> None:
        topo = topo or self.topology()
        pub = self.host_key.export_public_key().decode().strip()
        lines = sorted({f"[{ep.host}]:{ep.port} {pub}" for ep in topo.all_endpoints() if ep.port})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")


def load_or_create_host_key(path: Path) -> asyncssh.SSHKey:
    if path.exists():
        return asyncssh.read_private_key(str(path))
    key = asyncssh.generate_private_key("ssh-ed25519")
    path.parent.mkdir(parents=True, exist_ok=True)
    key.write_private_key(str(path))
    os.chmod(path, 0o600)
    return key


async def wait_forever() -> None:
    await asyncio.Event().wait()
