"""The demo morning end to end: 33 real SFTP servers, 22 passes on a simulated clock."""

from handoff import generator as gen
from handoff.simulate import fresh_settings, run_morning
from handoff.topology import default_topology

from conftest import DAY


async def test_demo_morning(tmp_path):
    topo = default_topology()
    s = fresh_settings(tmp_path, retry_backoff_s=0.01)
    res = await run_morning(s, topo, gen.demo_scenario(DAY, topo), log=None)
    assert res["passes"] == 22
    assert res["clean_scheduled"] == res["clean_branches"] == 13
    assert res["scheduled_by_deadline"] == 16
    assert res["classified_correctly"] == res["injected"] == 11
    assert res["false_alarms_on_clean_branches"] == {}
    assert res["duplicate_jobs"] == 0
    assert res["destination_deliveries_counted_by_servers"] == 2 * res["scheduled_total"]
    assert sorted(res["needs_a_person"]) == ["BR03", "BR08", "BR11", "BR17", "BR19", "BR20", "BR22", "BR23"]


async def test_random_morning_delivers_every_clean_branch(tmp_path):
    topo = default_topology()
    s = fresh_settings(tmp_path, retry_backoff_s=0.01)
    sc = gen.random_scenario(DAY, topo, seed=7)
    res = await run_morning(s, topo, sc, log=None)
    assert res["clean_scheduled"] == res["clean_branches"]
    assert res["duplicate_jobs"] == 0
