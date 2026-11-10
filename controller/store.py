"""The controller's only durable state: one SQLite file (WAL mode).

Why SQLite: Platform already runs Postgres; this schema is plain SQL and ports 1:1. For the
assignment a single file keeps `run.sh` dependency-free and makes crash tests trivial. Nothing
the controller knows lives only in memory: every in-memory structure is either rebuilt from
these tables on start, or is a cache of them.

Tables
  kv            cursors and small flags (last ingested chunk, bootstrap done, sim origin)
  leads         lead_id -> feature code, campaign day, concluded flag (the lead/feature join)
  cubes         per (workflow, kind, day) compressed numpy count arrays (see ingest.py)
  configs       mirror of every config version we have seen (who/when/why)
  proposals     every change we attempted: approval requests and auto-applies, full lifecycle
  decisions     append-only audit log: every detection, test result, proposal, apply, rollback
  exp_state     learned experiment state per workflow (rules, dropped/decayed arms)
  wf_state      misc per-workflow state (DID health status, plan day, incident state)
  alerts        things a human may need to look at, with severity page|ticket|info
  metrics       loop health per tick (ingest rate, lag, memory, credits)
  spend         every credit-costing call (LLM / validation), intent-logged
"""
from __future__ import annotations

import io
import json
import sqlite3
import time
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS leads (lead_id TEXT PRIMARY KEY, wf TEXT, code INTEGER, day INTEGER,
                                  campaign TEXT, concluded INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS leads_wf_day ON leads(wf, day);
CREATE TABLE IF NOT EXISTS cubes (wf TEXT, kind TEXT, day INTEGER, blob BLOB, PRIMARY KEY (wf, kind, day));
CREATE TABLE IF NOT EXISTS configs (wf TEXT, version INTEGER, actor TEXT, minute INTEGER, changed_dims TEXT,
                                    proposal_id TEXT, approval_id TEXT, config TEXT, PRIMARY KEY (wf, version));
CREATE TABLE IF NOT EXISTS proposals (proposal_id TEXT PRIMARY KEY, wf TEXT, kind TEXT, channel TEXT,
                                      status TEXT, approval_id TEXT, base_version INTEGER,
                                      created_minute INTEGER, decided_minute INTEGER, applied_version INTEGER,
                                      body TEXT, reasons TEXT, updated_minute INTEGER, supersedes TEXT);
CREATE INDEX IF NOT EXISTS proposals_wf ON proposals(wf, status);
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, minute INTEGER, wf TEXT, kind TEXT,
                                      summary TEXT, detail TEXT, proposal_id TEXT, wall REAL);
CREATE INDEX IF NOT EXISTS decisions_wf ON decisions(wf, minute);
CREATE TABLE IF NOT EXISTS exp_state (wf TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS wf_state (wf TEXT PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, minute INTEGER, wf TEXT, severity TEXT,
                                   code TEXT, message TEXT, resolved_minute INTEGER);
CREATE TABLE IF NOT EXISTS metrics (minute INTEGER PRIMARY KEY, data TEXT);
CREATE TABLE IF NOT EXISTS spend (id INTEGER PRIMARY KEY AUTOINCREMENT, minute INTEGER, wf TEXT, kind TEXT,
                                  request TEXT, response TEXT, credits REAL, status TEXT);
"""


def dumps(o) -> str:
    return json.dumps(o, separators=(",", ":"), default=_default, sort_keys=True)


def _default(o):
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(type(o))


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), isolation_level=None, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")  # an ack'd commit survives a kill -9
        self.db.executescript(SCHEMA)
        self._in_tx = False

    # ---- transactions: one `with store.tx():` = one atomic commit
    def tx(self):
        store = self

        class _Tx:
            def __enter__(self_):
                if store._in_tx:
                    self_.nested = True
                    return store
                self_.nested = False
                store.db.execute("BEGIN IMMEDIATE")
                store._in_tx = True
                return store

            def __exit__(self_, et, ev, tb):
                if self_.nested:
                    return False
                store._in_tx = False
                store.db.execute("ROLLBACK" if et else "COMMIT")
                return False
        return _Tx()

    def x(self, sql: str, args=()):
        return self.db.execute(sql, args)

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        cur = self.db.execute(sql, args)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def q1(self, sql: str, args=()):
        r = self.q(sql, args)
        return r[0] if r else None

    # ---- kv
    def get(self, k, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO kv(k, v) VALUES (?, ?)", (k, dumps(v)))

    # ---- json-blob state rows
    def get_state(self, table: str, wf: str, default=None):
        r = self.db.execute(f"SELECT data FROM {table} WHERE wf=?", (wf,)).fetchone()
        return json.loads(r[0]) if r else (default if default is not None else {})

    def put_state(self, table: str, wf: str, data):
        self.db.execute(f"INSERT OR REPLACE INTO {table}(wf, data) VALUES (?, ?)", (wf, dumps(data)))

    # ---- cubes
    def put_cube(self, wf: str, kind: str, day: int, arr: np.ndarray):
        buf = io.BytesIO()
        np.savez_compressed(buf, a=arr)
        self.db.execute("INSERT OR REPLACE INTO cubes(wf, kind, day, blob) VALUES (?,?,?,?)",
                        (wf, kind, day, buf.getvalue()))

    def load_cubes(self):
        for wf, kind, day, blob in self.db.execute("SELECT wf, kind, day, blob FROM cubes"):
            yield wf, kind, day, np.load(io.BytesIO(blob))["a"]

    # ---- audit
    def decide(self, minute: int, wf: str | None, kind: str, summary: str, detail=None, proposal_id=None):
        self.db.execute("INSERT INTO decisions(minute, wf, kind, summary, detail, proposal_id, wall) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (minute, wf, kind, summary, dumps(detail or {}), proposal_id, time.time()))

    def alert(self, minute: int, wf: str | None, severity: str, code: str, message: str):
        """De-duplicated: an open alert with the same (wf, code) is not raised twice."""
        open_ = self.db.execute("SELECT id FROM alerts WHERE wf IS ? AND code=? AND resolved_minute IS NULL",
                                (wf, code)).fetchone()
        if open_:
            return
        self.db.execute("INSERT INTO alerts(minute, wf, severity, code, message) VALUES (?,?,?,?,?)",
                        (minute, wf, severity, code, message))

    def resolve(self, minute: int, wf: str | None, code: str):
        self.db.execute("UPDATE alerts SET resolved_minute=? WHERE wf IS ? AND code=? AND resolved_minute IS NULL",
                        (minute, wf, code))
