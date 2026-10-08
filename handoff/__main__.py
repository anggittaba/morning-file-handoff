"""Command line: python -m handoff <command>"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from .settings import ROOT, Settings, load_dotenv

DEMO_DAY = date(2026, 10, 8)


def _topology(settings: Settings):
    from .topology import load_topology
    return load_topology(settings.topology, settings.branches)


def cmd_topology(args) -> None:
    from .topology import default_topology, save_topology
    s = Settings()
    save_topology(default_topology(), s.topology, s.branches)
    print(f"wrote {s.topology} and {s.branches}")


def _write_results(res: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(res, indent=2) + "\n")
    print(f"results: {path}")


def cmd_simulate(args) -> None:
    from . import generator as gen
    from .llm import provider_from_env
    from .simulate import run_morning
    s = Settings()
    topo = _topology(s)
    day = date.fromisoformat(args.date)
    sc = gen.demo_scenario(day, topo)
    until = datetime.combine(day, datetime.strptime(args.until, "%H:%M").time()) if args.until else None
    res = asyncio.run(run_morning(s, topo, sc, provider_from_env(), until=until))
    if args.out:
        _write_results(res, Path(args.out))
    else:
        print(json.dumps({k: v for k, v in res.items() if k != "classification"}, indent=2))


def cmd_batch(args) -> None:
    from .simulate import run_batch
    s = Settings()
    res = asyncio.run(run_batch(s.var / "batch", _topology(s), args.mornings, args.seed, date.fromisoformat(args.date)))
    if args.out:
        _write_results(res, Path(args.out))
    acc = res["correct"] / res["injected"] if res["injected"] else 0
    print(f"{res['correct']}/{res['injected']} injected failures classified correctly ({acc:.0%})")


def _engine(s: Settings):
    from . import db
    from .engine import Engine
    from .llm import provider_from_env
    from .topology import load_credentials
    return Engine(db.connect(s.db), _topology(s), s, load_credentials(s.credentials), provider_from_env())


def cmd_run(args) -> None:
    from .clock import now
    s = Settings()
    res = asyncio.run(_engine(s).run_pass(now(), "manual"))
    print(json.dumps(res, indent=2))


def cmd_serve(args) -> None:
    import uvicorn
    from .web import create_app
    s = Settings()
    app = create_app(_engine(s), loop=not args.no_loop)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def cmd_demo(args) -> None:
    """Simulated servers + accelerated clock + dashboard, all in one process."""
    import uvicorn
    from . import generator as gen
    from . import db
    from .clock import CLOCK
    from .engine import Engine
    from .llm import provider_from_env
    from .simulate import SimEnv
    from .web import create_app
    s = Settings()
    topo = _topology(s)
    day = date.fromisoformat(args.date)
    start = datetime.combine(day, datetime.strptime(args.start, "%H:%M").time())
    sc = gen.demo_scenario(day, topo)
    for p in (s.db, Path(str(s.db) + "-wal"), Path(str(s.db) + "-shm")):
        if p.exists():
            p.unlink()
    env = SimEnv(s, topo, sc)
    CLOCK.accelerate(start, args.speed)

    async def main():
        live = await env.start(CLOCK.now())
        engine = Engine(db.connect(s.db), live, s, env.client_credentials, provider_from_env())
        app = create_app(engine, loop=True, sim_env=env)
        print(f"Dashboard: http://{args.host}:{args.port}  (simulated clock from {args.start}, x{args.speed:g})")
        print(f"SFTP servers on 127.0.0.1 ports {topo.staging[0].port}-{max(e.port for e in topo.all_endpoints())}; "
              f"passwords in {s.sim / 'server_credentials.json'}")
        server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
        try:
            await server.serve()
        finally:
            await env.stop()

    asyncio.run(main())


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="python -m handoff")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("topology", help="write the default 9 staging / 24 destination topology to config/")
    sim = sub.add_parser("simulate", help="run the demo morning headless against local SFTP servers")
    sim.add_argument("--date", default=DEMO_DAY.isoformat())
    sim.add_argument("--until", help="stop after the pass at this time, e.g. 06:50 (leaves the DB mid-morning)")
    sim.add_argument("--out", help="write results JSON here")
    b = sub.add_parser("batch", help="run N random mornings and measure the classifier")
    b.add_argument("--mornings", type=int, default=30)
    b.add_argument("--seed", type=int, default=100)
    b.add_argument("--date", default=DEMO_DAY.isoformat())
    b.add_argument("--out")
    sub.add_parser("run", help="one pass now against the configured endpoints")
    sv = sub.add_parser("serve", help="dashboard + scheduled passes against the configured endpoints")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--no-loop", action="store_true", help="dashboard only, no scheduled passes")
    d = sub.add_parser("demo", help="simulated servers + fast clock + dashboard")
    d.add_argument("--date", default=DEMO_DAY.isoformat())
    d.add_argument("--start", default="05:58")
    d.add_argument("--speed", type=float, default=10.0, help="simulated seconds per real second")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)
    {"topology": cmd_topology, "simulate": cmd_simulate, "batch": cmd_batch, "run": cmd_run, "serve": cmd_serve,
     "demo": cmd_demo}[args.cmd](args)


if __name__ == "__main__":
    main(sys.argv[1:])
