import sqlite3

import pytest

from handoff import actions, db, scheduler
from handoff import exportfile as ef

from conftest import DAY, at


async def test_happy_path_audit_trail(env):
    data, _ = env.put_pair("BR01")
    res = await env.engine.run_pass(at(6, 0))
    assert env.row("BR01")["status"] == "scheduled"
    assert env.dest_file("BR01").read_bytes() == data
    assert env.events("BR01") == [("discover", "ok"), ("validate", "ok"), ("transfer", "ok"), ("verify", "ok"),
                                  ("schedule", "ok")]
    assert res["scheduled"] == 1
    job = db.jobs(env.con, DAY)[0]
    assert job["branch_id"] == "BR01" and job["run_at"].endswith("08:00:00")


async def test_rerun_never_double_sends(env):
    env.put_pair("BR01")
    await env.engine.run_pass(at(6, 0))
    before = len(db.events(env.con, DAY, "BR01"))
    await env.engine.run_pass(at(6, 5))
    await env.engine.run_pass(at(6, 10))
    assert env.farm.stats["dest-br01"]["deliveries"] == 2  # the CSV and its control file, once
    assert len(db.jobs(env.con, DAY)) == 1
    assert len(db.events(env.con, DAY, "BR01")) == before


async def test_crash_recovery_does_not_resend(env, tmp_path):
    """A fresh database (as after a crash) finds the file already delivered and does not upload it again."""
    env.put_pair("BR01")
    await env.engine.run_pass(at(6, 0))
    env.engine.con = db.connect(tmp_path / "fresh.db")
    env.con = env.engine.con
    await env.engine.run_pass(at(6, 5))
    assert env.farm.stats["dest-br01"]["deliveries"] == 2
    assert ("transfer", "info") in env.events("BR01")
    assert env.row("BR01")["status"] == "scheduled"


async def test_waiting_then_late_then_missing(env):
    await env.engine.run_pass(at(6, 0))
    assert env.row("BR03")["status"] == "waiting"
    await env.engine.run_pass(at(6, 25))  # BR03 expected by 06:20
    assert env.row("BR03")["status"] == "late"
    await env.engine.run_pass(at(7, 30))
    r = env.row("BR03")
    assert r["status"] == "failed" and "not found by the deadline" in r["failure_detail"]


async def test_late_file_is_still_delivered(env):
    await env.engine.run_pass(at(6, 25))
    assert env.row("BR03")["status"] == "late"
    env.put_pair("BR03")
    await env.engine.run_pass(at(6, 30))
    r = env.row("BR03")
    assert r["status"] == "scheduled" and r["failure_detail"] is None


async def test_waits_for_control_file(env):
    import random
    from handoff import generator as gen
    d, c = gen.make_pair(random.Random(1), "BR02", DAY, 10, at(5, 0))
    env.put("staging-02", ef.csv_name("BR02", DAY), d)
    await env.engine.run_pass(at(6, 0))
    assert env.row("BR02")["status"] == "found" and env.row("BR02")["note"] == "waiting for control file"
    env.put("staging-02", ef.ctl_name("BR02", DAY), c)
    await env.engine.run_pass(at(6, 5))
    assert env.row("BR02")["status"] == "scheduled"


@pytest.mark.parametrize("mangle,stage", [
    (lambda d: b"", "validate"),
    (lambda d: d[: len(d) // 2], "validate"),
    (lambda d: d.replace(b",SALE,", b",SAL3,", 1), "validate"),
])
async def test_bad_files_are_not_delivered(env, mangle, stage):
    import random
    from handoff import generator as gen
    d, c = gen.make_pair(random.Random(2), "BR05", DAY, 40, at(5, 0))
    env.put_pair("BR05", data=mangle(d), ctl=c)
    await env.engine.run_pass(at(6, 0))
    r = env.row("BR05")
    assert r["status"] == "failed" and r["failure_stage"] == stage
    assert not env.dest_file("BR05").exists()


async def test_corrected_file_is_picked_up(env):
    d, c = env.put_pair("BR05")
    env.put_pair("BR05", data=b"", ctl=c)
    await env.engine.run_pass(at(6, 0))
    assert env.row("BR05")["status"] == "failed"
    env.put_pair("BR05", data=d, ctl=c)
    await env.engine.run_pass(at(6, 5))
    assert env.row("BR05")["status"] == "scheduled"


async def test_identical_duplicate_is_sent_once(env):
    d, c = env.put_pair("BR06")
    env.put_pair("BR06", endpoint="staging-09", data=d, ctl=c)
    await env.engine.run_pass(at(6, 0))
    assert env.row("BR06")["status"] == "scheduled"
    assert env.farm.stats["dest-br06"]["deliveries"] == 2
    assert ("discover", "info") in env.events("BR06")


async def test_conflicting_duplicates_need_a_person(env):
    env.put_pair("BR06", rows=20)
    env.put_pair("BR06", rows=25, endpoint="staging-09")
    await env.engine.run_pass(at(6, 0))
    r = env.row("BR06")
    assert r["status"] == "failed" and "different contents" in r["failure_detail"]


async def test_different_copy_after_delivery_needs_a_person(env):
    """A second, different copy that shows up after delivery is flagged; nothing is re-sent automatically."""
    env.put_pair("BR06", rows=20)
    await env.engine.run_pass(at(6, 0))
    assert env.row("BR06")["status"] == "scheduled"
    env.put_pair("BR06", rows=25, endpoint="staging-09")
    await env.engine.run_pass(at(6, 5))
    r = env.row("BR06")
    assert r["status"] == "failed" and r["human_gate"] == 1
    assert "appeared after delivery" in r["failure_detail"]
    assert env.farm.stats["dest-br06"]["deliveries"] == 2, "no automatic re-send"
    assert len(db.jobs(env.con, DAY)) == 1
    from handoff.classify import classify
    assert classify(r["failure_stage"], r["failure_detail"]).category == "duplicate_conflict"
    await env.engine.run_pass(at(6, 10))
    assert env.farm.stats["dest-br06"]["deliveries"] == 2


async def test_login_failure_waits_for_a_person(make_env):
    e = await make_env(wrong_password=["dest-br07"])
    e.put_pair("BR07")
    await e.engine.run_pass(at(6, 0))
    r = e.row("BR07")
    assert r["status"] == "failed" and r["human_gate"] == 1
    tries = e.farm.stats["dest-br07"]["auth_failures"]
    await e.engine.run_pass(at(6, 5))
    assert e.farm.stats["dest-br07"]["auth_failures"] == tries, "no automatic login retry"
    # a person fixes the password and authorises one retry
    e.engine.credentials["dest-br07"] = e.passwords["dest-br07"]
    actions.authorise_retry(e.con, DAY, "BR07", "tester", at(6, 7), "password updated")
    await e.engine.run_pass(at(6, 10))
    assert e.row("BR07")["status"] == "scheduled"
    assert ("human", "retry") in e.events("BR07")


@pytest.mark.parametrize("behaviour", ["disk_full", "permission_denied"])
async def test_destination_errors_wait_for_a_person(make_env, behaviour):
    e = await make_env(behaviours={"dest-br08": {behaviour}})
    e.put_pair("BR08")
    await e.engine.run_pass(at(6, 0))
    r = e.row("BR08")
    assert r["status"] == "failed" and r["human_gate"] == 1 and r["failure_stage"] == "upload"
    assert db.health(e.con)["dest-br08"]["status"] in ("disk_full", "no_write")


async def test_transient_error_is_retried(make_env):
    e = await make_env(behaviours={"dest-br09": {"transient_once"}})
    e.put_pair("BR09")
    await e.engine.run_pass(at(6, 0))
    r = e.row("BR09")
    assert r["status"] == "scheduled" and r["attempts"] == 2


async def test_unreachable_staging_server_is_reported(env):
    await env.farm.stop_endpoint("staging-03")
    res = await env.engine.run_pass(at(6, 0))
    assert any("staging-03" in err for err in res["errors"])
    assert db.health(env.con)["staging-03"]["status"] == "unreachable"


async def test_audit_log_is_append_only(env):
    env.put_pair("BR01")
    await env.engine.run_pass(at(6, 0))
    with pytest.raises(sqlite3.DatabaseError):
        env.con.execute("UPDATE events SET detail='x'")
    with pytest.raises(sqlite3.DatabaseError):
        env.con.execute("DELETE FROM events")


async def test_scheduler_never_queues_twice(env):
    a, created_a = scheduler.enqueue(env.con, DAY, "BR01", "f.csv", "dest-br01", "/inbound/f.csv", "08:00", at(6, 0))
    b, created_b = scheduler.enqueue(env.con, DAY, "BR01", "f.csv", "dest-br01", "/inbound/f.csv", "08:00", at(6, 5))
    assert created_a and not created_b and a["id"] == b["id"]
    assert scheduler.mark_due(env.con, at(8, 1)) == 1


async def test_human_actions(env):
    await env.engine.run_pass(at(7, 30))
    with pytest.raises(actions.ActionError):
        actions.skip(env.con, DAY, "BR03", "tester", at(7, 31), " ")
    actions.skip(env.con, DAY, "BR03", "tester", at(7, 31), "branch closed today")
    assert env.row("BR03")["status"] == "skipped"
    await env.engine.run_pass(at(7, 35))
    assert env.row("BR03")["status"] == "skipped", "the engine leaves closed branches alone"
    actions.reopen(env.con, DAY, "BR03", "tester", at(7, 36))
    actions.mark_handled(env.con, DAY, "BR03", "tester", at(7, 37), "sent by hand")
    assert [e for e in env.events("BR03") if e[0] == "human"] == [("human", "skipped"), ("human", "reopened"),
                                                                     ("human", "handled")]


def test_pass_times():
    times = scheduler.pass_times(DAY, (6, 0), (7, 30), 5)
    assert times[0] == at(6, 0) and times[-1] == at(7, 30) and len(times) == 19
