"""SFTP client side: list, read, and deliver with an atomic rename and a read-back check."""

from __future__ import annotations

import asyncio
import hashlib
import posixpath
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncssh

from .topology import Endpoint


@dataclass
class RemoteFile:
    name: str
    size: int
    mtime: float


class TransferError(Exception):
    """Raised with the stage ("connect", "upload", "verify") and the original error text."""

    def __init__(self, stage: str, error: BaseException | str, attempts: int = 1) -> None:
        self.stage = stage
        self.kind = type(error).__name__ if isinstance(error, BaseException) else "Error"
        self.text = str(error) or self.kind
        self.attempts = attempts
        super().__init__(f"{stage}: {self.kind}: {self.text}")


@asynccontextmanager
async def session(ep: Endpoint, password: str | None, known_hosts: str | None, timeout: float = 8.0):
    """Open an SSH connection and an SFTP session. Errors surface as TransferError("connect")."""
    try:
        conn = await asyncio.wait_for(asyncssh.connect(
            ep.host, ep.port, username=ep.username, password=password or "",
            known_hosts=known_hosts, client_keys=None, agent_path=None, preferred_auth="password",
            connect_timeout=timeout, keepalive_interval=0), timeout + 2)
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
        raise TransferError("connect", exc) from exc
    try:
        async with conn:
            sftp = await conn.start_sftp_client()
            yield sftp
    except asyncssh.Error:
        raise


async def list_dir(sftp: asyncssh.SFTPClient, path: str) -> list[RemoteFile]:
    out = []
    for entry in await sftp.readdir(path):
        name = entry.filename
        if name in (".", "..") or name.startswith("."):
            continue
        out.append(RemoteFile(name, entry.attrs.size or 0, float(entry.attrs.mtime or 0)))
    return out


async def read_file(sftp: asyncssh.SFTPClient, path: str) -> bytes:
    async with sftp.open(path, "rb") as f:
        return await f.read()


async def exists(sftp: asyncssh.SFTPClient, path: str) -> bool:
    try:
        await sftp.stat(path)
        return True
    except asyncssh.SFTPNoSuchFile:
        return False


async def deliver(sftp: asyncssh.SFTPClient, folder: str, name: str, data: bytes) -> dict:
    """Write to <name>.part, check size and SHA-256 by reading it back, then rename into place."""
    final = posixpath.join(folder, name)
    part = final + ".part"
    try:
        async with sftp.open(part, "wb") as f:
            await f.write(data)
    except asyncssh.SFTPError:
        try:  # best effort: do not leave a partial .part file behind
            await sftp.remove(part)
        except (asyncssh.Error, OSError):
            pass
        raise
    attrs = await sftp.stat(part)
    if attrs.size != len(data):
        raise TransferError("verify", f"size after upload is {attrs.size}, expected {len(data)}")
    back = await read_file(sftp, part)
    digest = hashlib.sha256(back).hexdigest()
    if digest != hashlib.sha256(data).hexdigest():
        raise TransferError("verify", "checksum after upload does not match")
    if await exists(sftp, final):
        await sftp.remove(final)
    try:
        await sftp.posix_rename(part, final)
    except (asyncssh.SFTPOpUnsupported, asyncssh.SFTPError):
        await sftp.rename(part, final)
    final_attrs = await sftp.stat(final)
    return {"bytes": final_attrs.size, "sha256": digest}
