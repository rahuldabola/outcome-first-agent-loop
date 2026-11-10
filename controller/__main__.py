"""CLI.

  python -m controller run     --url http://127.0.0.1:8700 --db state/ctl.db [--step 60]
  python -m controller view    --db state/ctl.db [--workflow ID]           # supervisor view (C11)
  python -m controller why     --db state/ctl.db --workflow ID --at "D3 14:00"   # audit (C12)
  python -m controller log     --db state/ctl.db [--workflow ID] [--kind K]    # decision log
  python -m controller health  --db state/ctl.db                              # loop health (C14)
"""
import argparse
import logging
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="controller")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--url", default="http://127.0.0.1:8700")
    r.add_argument("--db", default="state/controller.db")
    r.add_argument("--step", type=int, default=60)
    r.add_argument("--prefs")
    r.add_argument("--stop-at", type=int, help="crash test: hard-exit once sim minute >= this")
    for name in ("view", "why", "log", "health"):
        p = sub.add_parser(name)
        p.add_argument("--db", default="state/controller.db")
        p.add_argument("--workflow")
        if name == "why":
            p.add_argument("--at", required=True, help='"D3 14:00" or a sim minute')
        if name == "log":
            p.add_argument("--kind")
            p.add_argument("--limit", type=int, default=200)
        if name == "view":
            p.add_argument("--day", type=int)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.cmd == "run":
        from .loop import Controller
        Controller(a.url, a.db, a.step, a.prefs, a.stop_at).run()
        return
    from . import report
    print(getattr(report, a.cmd)(a))


if __name__ == "__main__":
    main()
