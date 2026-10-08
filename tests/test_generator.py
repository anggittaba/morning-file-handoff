from handoff import exportfile as ef
from handoff import generator as gen
from handoff.topology import default_topology

from conftest import DAY


def test_demo_scenario_covers_every_failure_type():
    sc = gen.demo_scenario(DAY, default_topology())
    assert set(sc.injected.values()) == set(gen.INJECTABLE)
    assert sc.offline and sc.wrong_password and sc.behaviours


def test_demo_scenario_is_deterministic():
    topo = default_topology()
    a, b = gen.demo_scenario(DAY, topo), gen.demo_scenario(DAY, topo)
    assert [(d.at, d.endpoint, d.name, d.data) for d in a.drops] == [(d.at, d.endpoint, d.name, d.data) for d in b.drops]


def test_clean_branch_files_validate():
    topo = default_topology()
    sc = gen.demo_scenario(DAY, topo)
    by_name = {}
    for d in sc.drops:
        by_name.setdefault(d.name, d.data)
    for b in topo.branches:
        if b.branch_id in sc.injected or b.branch_id == "BR16":
            continue
        data, ctl = by_name[ef.csv_name(b.branch_id, DAY)], by_name[ef.ctl_name(b.branch_id, DAY)]
        assert ef.validate(data, ctl, DAY).ok, b.branch_id


def test_random_scenarios_vary_and_label_branches():
    topo = default_topology()
    seen = set()
    for seed in range(20):
        sc = gen.random_scenario(DAY, topo, seed)
        assert 1 <= len(sc.injected) <= 5
        seen.update(sc.injected.values())
    assert len(seen) >= 8


def test_data_is_synthetic():
    sc = gen.demo_scenario(DAY, default_topology())
    text = b"".join(d.data for d in sc.drops[:10]).decode()
    assert "record_id,branch_id,business_date" in text
