import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from handoff import db
from handoff import generator as gen
from handoff.clock import CLOCK
from handoff.engine import Engine
from handoff.simulate import fresh_settings, run_morning
from handoff.topology import default_topology, load_credentials
from handoff.web import create_app

from conftest import DAY, at


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    var = tmp_path_factory.mktemp("web")
    topo = default_topology()
    s = fresh_settings(var, retry_backoff_s=0.01)
    asyncio.run(run_morning(s, topo, gen.demo_scenario(DAY, topo), until=at(6, 50), log=None))
    CLOCK.set_fixed(at(6, 50))
    engine = Engine(db.connect(s.db), topo, s, load_credentials(s.credentials))
    with TestClient(create_app(engine, loop=False)) as c:
        yield c
    CLOCK.real()


def test_board_shows_all_branches_and_countdown(client):
    html = client.get("/").text
    assert html.count('class="tile ') == 24
    assert "40:00" in html and "At risk" in html


def test_needs_a_person_filter(client):
    html = client.get("/?f=human").text
    tiles = re.findall(r'class="tile tile--(\w+)', html)
    assert tiles and set(tiles) <= {"failed", "late"}
    assert len(tiles) == 10


def test_branch_detail_has_explanation_and_timeline(client):
    html = client.get("/b/BR20").text
    assert "Destination rejected the login" in html
    assert "rule-based explainer" in html
    assert "Authorise one retry" in html
    assert "PermissionDenied" in html


def test_skip_needs_a_reason(client):
    r = client.post("/b/BR19/skip", data={"reason": ""}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    r = client.post("/b/BR19/skip", data={"reason": "branch closed for stocktake"}, follow_redirects=True)
    assert "status--skipped" in r.text and "branch closed for stocktake" in r.text


def test_mark_handled_and_reopen(client):
    r = client.post("/b/BR08/handled", data={"note": "re-sent by hand"}, follow_redirects=True)
    assert "status--handled" in r.text and "re-sent by hand" in r.text
    r = client.post("/b/BR08/reopen", follow_redirects=True)
    assert "status--failed" in r.text and "reopened" in r.text


def test_run_log_and_scheduler(client):
    assert "Passes" in client.get("/runs").text
    html = client.get("/scheduler").text
    assert html.count("<tr>") >= 14


def test_unknown_branch_404(client):
    assert client.get("/b/BR99").status_code == 404


def test_api_board(client):
    data = client.get("/api/board").json()
    assert len(data["branches"]) == 24 and data["deadline"].endswith("07:30:00")
