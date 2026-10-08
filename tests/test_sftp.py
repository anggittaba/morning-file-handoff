"""Real SFTP transfers against the local asyncssh servers."""

import asyncssh
import pytest

from handoff import sftp_client as sc
from handoff.sftp_farm import SftpFarm

from conftest import Env


async def test_deliver_and_verify(env):
    ep = env.topo.endpoint("dest-br01")
    async with sc.session(ep, env.client_pw[ep.name], str(env.settings.known_hosts)) as sftp:
        out = await sc.deliver(sftp, "/inbound", "x.csv", b"a,b\n1,2\n")
        assert out["bytes"] == 8
        assert sorted(f.name for f in await sc.list_dir(sftp, "/inbound")) == ["x.csv"]
    assert (env.roots / "dest-br01" / "inbound" / "x.csv").read_bytes() == b"a,b\n1,2\n"
    assert env.farm.stats["dest-br01"]["deliveries"] == 1


async def test_wrong_password_is_auth_error(make_env):
    e = await make_env(wrong_password=["dest-br02"])
    ep = e.topo.endpoint("dest-br02")
    with pytest.raises(sc.TransferError) as info:
        async with sc.session(ep, e.client_pw[ep.name], str(e.settings.known_hosts)):
            pass
    assert info.value.stage == "connect" and info.value.kind == "PermissionDenied"


async def test_unknown_host_key_is_rejected(env, tmp_path):
    other = asyncssh.generate_private_key("ssh-ed25519")
    kh = tmp_path / "other_known_hosts"
    ep = env.topo.endpoint("dest-br01")
    kh.write_text(f"[{ep.host}]:{ep.port} {other.export_public_key().decode().strip()}\n")
    with pytest.raises(sc.TransferError):
        async with sc.session(ep, env.client_pw[ep.name], str(kh)):
            pass


@pytest.mark.parametrize("behaviour,error", [("disk_full", "No space left on device"),
                                             ("permission_denied", "Permission denied")])
async def test_destination_write_errors(make_env, behaviour, error):
    e = await make_env(behaviours={"dest-br03": {behaviour}})
    ep = e.topo.endpoint("dest-br03")
    async with sc.session(ep, e.client_pw[ep.name], str(e.settings.known_hosts)) as sftp:
        with pytest.raises(asyncssh.SFTPError) as info:
            await sc.deliver(sftp, "/inbound", "x.csv", b"data\n")
        assert error in str(info.value)
    assert not list((e.roots / "dest-br03" / "inbound").glob("*")), "no partial file left behind"


async def test_transient_drop_then_success(make_env):
    e = await make_env(behaviours={"dest-br04": {"transient_once"}})
    ep = e.topo.endpoint("dest-br04")
    with pytest.raises((asyncssh.Error, OSError)):
        async with sc.session(ep, e.client_pw[ep.name], str(e.settings.known_hosts)) as sftp:
            await sc.deliver(sftp, "/inbound", "x.csv", b"data\n")
    async with sc.session(ep, e.client_pw[ep.name], str(e.settings.known_hosts)) as sftp:
        assert (await sc.deliver(sftp, "/inbound", "x.csv", b"data\n"))["bytes"] == 5


async def test_chroot_keeps_endpoints_apart(env):
    env.put("staging-01", "secret.csv", b"x\n")
    ep = env.topo.endpoint("staging-02")
    async with sc.session(ep, env.client_pw[ep.name], str(env.settings.known_hosts)) as sftp:
        assert await sc.list_dir(sftp, "/exports") == []
        assert not await sc.exists(sftp, "/../staging-01/exports/secret.csv")
