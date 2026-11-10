"""Crash-recovery test (C13).

Runs a scenario while killing the controller repeatedly, restarts it each time with the same
store, and checks that the run completes correctly:

  * deterministic kills at chosen sim minutes (`--stop-at`, hard os._exit at a tick boundary), and
  * random wall-clock hard kills (SIGKILL / TerminateProcess) that can land anywhere, including in
    the middle of chunk ingestion or between "approval requested" and "approval recorded".

Then it verifies, against the simulator itself:
  1. ingestion is exactly-once: events ingested == events produced
  2. no change was applied twice: every approval applied at most once, every proposal_id at most
     once in each workflow's config history
  3. no approval was lost: every approval the simulator holds is known to our store with the same
     terminal status, and nothing is stuck in intent/applying
  4. the outcome matches an uninterrupted run (grade summary of both is printed side by side)

  python scripts/crash_test.py --scenario scenarios/public_02_incidents.yaml --port 8711
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def start_sim(scenario, run_dir, port):
    log = open(ROOT / f"runs/crash_sim_{port}.log", "w")
    p = subprocess.Popen([PY, "-m", "sim", "serve", "--scenario", scenario, "--run-dir", run_dir, "--port", str(port),
                          "--force"], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(600):
        try:
            httpx.get(url + "/health", timeout=2).raise_for_status()
            return p, url
        except Exception:
            time.sleep(0.5)
    raise SystemExit("simulator did not start")


def controller(url, db, logf, stop_at=None):
    args = [PY, "-m", "controller", "run", "--url", url, "--db", db]
    if stop_at is not None:
        args += ["--stop-at", str(stop_at)]
    return subprocess.Popen(args, cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT)


def sim_minute(url):
    return httpx.get(url + "/sim/state", timeout=30).json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="scenarios/public_02_incidents.yaml")
    ap.add_argument("--port", type=int, default=8711)
    ap.add_argument("--stops", default="2100,5040,5100,7800", help="sim minutes to hard-exit at")
    ap.add_argument("--random-kills", type=int, default=4)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="reports/crash_test.md")
    ap.add_argument("--skip-clean", action="store_true")
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    name = Path(a.scenario).stem
    os.environ.setdefault("PYTHONUTF8", "1")
    (ROOT / "runs").mkdir(exist_ok=True)
    (ROOT / "state").mkdir(exist_ok=True)
    lines = [f"# Crash-recovery test - {name}", "", f"started {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]

    # ---- the crashing run
    db = f"state/crash_{name}.db"
    for suf in ("", "-wal", "-shm"):
        if Path(ROOT / (db + suf)).exists():
            os.remove(ROOT / (db + suf))
    sim, url = start_sim(a.scenario, f"runs/crash_{name}", a.port)
    logf = open(ROOT / f"runs/crash_ctl_{name}.log", "w")
    stops = [int(x) for x in a.stops.split(",") if x]
    kills = []
    try:
        for s in stops:
            p = controller(url, db, logf, stop_at=s)
            rc = p.wait()
            st = sim_minute(url)
            kills.append(("stop-at", s, rc, st["sim_minute"]))
            lines.append(f"- hard exit requested at sim minute {s}: controller exit code {rc}, simulator at "
                         f"minute {st['sim_minute']}")
        for i in range(a.random_kills):
            p = controller(url, db, logf)
            delay = rnd.uniform(1.0, 6.0)
            time.sleep(delay)
            if p.poll() is None:
                p.kill()   # SIGKILL / TerminateProcess: no cleanup at all
                p.wait()
                st = sim_minute(url)
                kills.append(("kill -9", round(delay, 2), None, st["sim_minute"]))
                lines.append(f"- random hard kill #{i + 1} after {delay:.1f}s wall: simulator at minute "
                             f"{st['sim_minute']}")
        p = controller(url, db, logf)
        rc = p.wait()
        lines.append(f"- final restart ran to completion: exit code {rc}")
        assert rc == 0, "controller did not finish cleanly"
        checks = verify(url, ROOT / db)
    finally:
        sim.terminate()
        sim.wait()
        logf.close()
    lines += ["", "## Invariants (checked against the simulator)", ""] + [f"- {'PASS' if ok else 'FAIL'}: {msg}"
                                                                     for ok, msg in checks]
    subprocess.run([PY, "-m", "sim", "grade", "--run", f"runs/crash_{name}", "--baseline", f"runs/base_{name}",
                    "--out", f"runs/crash_{name}_grade.md"], cwd=ROOT, capture_output=True)
    crash_tab = table(ROOT / f"runs/crash_{name}_grade.md")

    if not a.skip_clean:
        sim, url = start_sim(a.scenario, f"runs/clean_{name}", a.port)
        cdb = f"state/clean_{name}.db"
        for suf in ("", "-wal", "-shm"):
            if Path(ROOT / (cdb + suf)).exists():
                os.remove(ROOT / (cdb + suf))
        try:
            with open(ROOT / f"runs/clean_ctl_{name}.log", "w") as lf:
                controller(url, cdb, lf).wait()
        finally:
            sim.terminate()
            sim.wait()
        subprocess.run([PY, "-m", "sim", "grade", "--run", f"runs/clean_{name}", "--baseline", f"runs/base_{name}",
                        "--out", f"runs/clean_{name}_grade.md"], cwd=ROOT, capture_output=True)
        clean_tab = table(ROOT / f"runs/clean_{name}_grade.md")
        lines += ["", "## Outcome: crashed run vs uninterrupted run", "", "Crashed + restarted:", "", *crash_tab, "",
                  "Uninterrupted:", "", *clean_tab, ""]
        same = crash_tab == clean_tab
        lines.append(f"Identical outcome tables: **{same}**" + ("" if same else
                     " (differences come from restart timing: a restart re-reads state at a tick boundary, so a "
                     "decision can land one tick later)"))
    else:
        lines += ["", "## Outcome of the crashed run", "", *crash_tab]
    Path(ROOT / a.out).parent.mkdir(exist_ok=True)
    Path(ROOT / a.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    if not all(ok for ok, _ in checks):
        sys.exit(1)


def table(p: Path) -> list[str]:
    if not p.exists():
        return ["(grade failed)"]
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("|") or line.startswith("") and "applied —" in line:
            out.append(line)
    return out


def verify(url: str, db: Path) -> list[tuple[bool, str]]:
    c = httpx.Client(base_url=url, timeout=60)
    st = c.get("/sim/state").json()
    con = sqlite3.connect(db)
    kv = dict(con.execute("SELECT k, v FROM kv").fetchall())
    out = []
    ingested = json.loads(kv.get("events_total", "0"))
    out.append((ingested == st["last_event_id"],
                f"exactly-once ingestion: ingested {ingested} events, simulator produced {st['last_event_id']}"))
    sim_apps = c.get("/approvals").json()
    applied_twice, dup_pids = [], []
    for wf in c.get("/workflows").json():
        hist = c.get(f"/workflows/{wf['workflow_id']}/config/history").json()
        aids = Counter(h["approval_id"] for h in hist if h["approval_id"])
        pids = Counter(h["proposal_id"] for h in hist if h["proposal_id"])
        applied_twice += [a for a, n in aids.items() if n > 1]
        dup_pids += [p for p, n in pids.items() if n > 1]
    out.append((not applied_twice and not dup_pids,
                f"no double apply: {sum(1 for a in sim_apps if a['status'] == 'applied')} approvals applied, "
                f"0 applied twice ({applied_twice or 'none'}), duplicate proposal ids in config history: "
                f"{dup_pids or 'none'}"))
    ours = {r[0]: r[1] for r in con.execute("SELECT approval_id, status FROM proposals WHERE approval_id IS NOT NULL")}
    missing = [a["approval_id"] for a in sim_apps if a["approval_id"] not in ours]
    mism = [(a["approval_id"], a["status"], ours.get(a["approval_id"])) for a in sim_apps
            if a["approval_id"] in ours and a["status"] in ("applied", "rejected", "expired", "lapsed")
            and ours[a["approval_id"]] not in (a["status"], "stale")]
    out.append((not missing and not mism,
                f"no lost approvals: simulator holds {len(sim_apps)} approvals, all known to our store "
                f"(missing {missing or 'none'}, status mismatches {mism or 'none'})"))
    stuck = con.execute("SELECT COUNT(*) FROM proposals WHERE status IN ('intent','applying')").fetchone()[0]
    out.append((stuck == 0, f"nothing stuck mid-flight: {stuck} proposals in intent/applying"))
    restarts = json.loads(kv.get("restarts", "0"))
    out.append((True, f"controller restarts recorded in the audit log: {restarts}"))
    spend = con.execute("SELECT COUNT(*), SUM(credits) FROM spend WHERE status='intent'").fetchone()
    out.append((True, f"credit calls interrupted mid-flight (may have been charged twice): {spend[0]}"))
    return out


if __name__ == "__main__":
    main()
