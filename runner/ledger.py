"""ledger.py — the one root-owned transactional admission ledger + outbox (sqlite3).

box-job-admission v0.5.1 @ 270ae9c, §3.5 (at-most-once). Owned EXCLUSIVELY by the
privileged component (cmp_admitd); the unprivileged poller has no path to this DB.

Launch phases (Pi re-audit #4) make the pre-launch/post-start boundary durable:

    RESERVED  -> lock taken, nothing prepared yet
    PREPARED  -> verified request delivered as a root-owned read-only file
    STARTING  -> written immediately BEFORE the backend start call (the boundary)
    RUNNING   -> written immediately AFTER start returned
    {SUCCEEDED|FAILED|REJECTED|TIMEOUT|INDETERMINATE}  -> terminal

Only absence AFTER the start boundary (STARTING/RUNNING) is `indeterminate`; a crash
in RESERVED/PREPARED is a truthful pre-launch REJECTION, never "unknown launch".

Outbox rows carry two durable publication states (Pi re-audit #5): `local_committed`
(box-signed object written into the local mirror) and `remote_acked` (pushed to
origin AND the remote tip verified). A row is only ever acked after a verified push,
so a terminal event can never be silently absent remotely.
"""
import sqlite3, time, json
from contextlib import contextmanager

TERMINAL = {"SUCCEEDED", "FAILED", "REJECTED", "TIMEOUT", "INDETERMINATE"}
NONTERMINAL = {"RESERVED", "PREPARED", "STARTING", "RUNNING"}
POST_START = {"STARTING", "RUNNING"}          # crossing the start-call boundary

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger(
  request_digest TEXT PRIMARY KEY,
  job_id         TEXT NOT NULL UNIQUE,
  unit           TEXT,
  queue_commit   TEXT,
  input_path     TEXT,
  input_digest   TEXT,
  principal      TEXT,
  fingerprint    TEXT,
  state          TEXT NOT NULL,
  memory_peak    INTEGER,
  runtime_max    INTEGER,
  created_at     INTEGER,
  updated_at     INTEGER
);
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER);
CREATE TABLE IF NOT EXISTS jobseq(job_id TEXT PRIMARY KEY, seq INTEGER);
CREATE TABLE IF NOT EXISTS outbox(
  event_id        INTEGER PRIMARY KEY,
  kind            TEXT,
  job_id          TEXT,
  payload         TEXT,
  local_committed INTEGER NOT NULL DEFAULT 0,
  remote_acked    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS incidents(
  position TEXT PRIMARY KEY, kind TEXT, detail TEXT, event_id INTEGER
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path):
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _next(conn, name):
    conn.execute("INSERT INTO counters(name,value) VALUES(?,1) "
                 "ON CONFLICT(name) DO UPDATE SET value=value+1", (name,))
    return conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()["value"]


def meta_get(conn, key, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def meta_set(conn, key, value):
    with tx(conn):
        conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def get(conn, digest):
    return conn.execute("SELECT * FROM ledger WHERE request_digest=?", (digest,)).fetchone()


def id_digest(conn, job_id):
    r = conn.execute("SELECT request_digest FROM ledger WHERE job_id=?", (job_id,)).fetchone()
    return r["request_digest"] if r else None


def intents(conn):
    return conn.execute("SELECT * FROM ledger WHERE state IN "
                        "('RESERVED','PREPARED','STARTING','RUNNING')").fetchall()


def in_flight(conn):
    return conn.execute("SELECT 1 FROM ledger WHERE state IN "
                        "('RESERVED','PREPARED','STARTING','RUNNING') LIMIT 1").fetchone() is not None


def try_intent(conn, digest, job_id, unit, queue_commit, principal, fingerprint):
    """Durable at-most-once lock (§3.5). Returns REPLAY_TERMINAL | IN_FLIGHT |
    INTEGRITY | CREATED. On CREATED the row is in state RESERVED."""
    with tx(conn):
        row = conn.execute("SELECT state FROM ledger WHERE request_digest=?", (digest,)).fetchone()
        if row:
            return "REPLAY_TERMINAL" if row["state"] in TERMINAL else "IN_FLIGHT"
        prev = conn.execute("SELECT request_digest FROM ledger WHERE job_id=?", (job_id,)).fetchone()
        if prev and prev["request_digest"] != digest:
            return "INTEGRITY"
        now = int(time.time())
        try:
            conn.execute("INSERT INTO ledger(request_digest,job_id,unit,queue_commit,principal,"
                         "fingerprint,state,created_at,updated_at) VALUES(?,?,?,?,?,?, 'RESERVED', ?, ?)",
                         (digest, job_id, unit, queue_commit, principal, fingerprint, now, now))
        except sqlite3.IntegrityError:
            return "INTEGRITY"
        return "CREATED"


def set_phase(conn, digest, phase, input_path=None, input_digest=None):
    with tx(conn):
        if input_path is not None:
            conn.execute("UPDATE ledger SET state=?, input_path=?, input_digest=?, updated_at=? "
                         "WHERE request_digest=?", (phase, input_path, input_digest, int(time.time()), digest))
        else:
            conn.execute("UPDATE ledger SET state=?, updated_at=? WHERE request_digest=?",
                         (phase, int(time.time()), digest))


def _stage(conn, kind, job_id, builder):
    eid = _next(conn, "event_id")
    if kind == "job":
        conn.execute("INSERT INTO jobseq(job_id,seq) VALUES(?,1) "
                     "ON CONFLICT(job_id) DO UPDATE SET seq=seq+1", (job_id,))
        seq = conn.execute("SELECT seq FROM jobseq WHERE job_id=?", (job_id,)).fetchone()["seq"]
        body = builder(eid, seq)
    else:
        body = builder(eid)
    conn.execute("INSERT INTO outbox(event_id,kind,job_id,payload) VALUES(?,?,?,?)",
                 (eid, kind, job_id, json.dumps(body)))
    return body


def enqueue_job_event(conn, job_id, builder):
    with tx(conn):
        return _stage(conn, "job", job_id, builder)


def record_terminal(conn, digest, state, reason, builder, memory_peak=None, runtime_max=None):
    """Atomically commit terminal ledger state + its status event to the outbox
    (Pi #6). Idempotent: returns None if already terminal."""
    st = state.upper()
    with tx(conn):
        row = conn.execute("SELECT state,job_id FROM ledger WHERE request_digest=?", (digest,)).fetchone()
        if row is None or row["state"] in TERMINAL:
            return None
        body = _stage(conn, "job", row["job_id"], builder)
        conn.execute("UPDATE ledger SET state=?, memory_peak=?, runtime_max=?, updated_at=? "
                     "WHERE request_digest=?", (st, memory_peak, runtime_max, int(time.time()), digest))
        return body


def incident_once(conn, position, kind, detail, builder):
    """Sticky deduped fault (Pi #5): one incident per offending position."""
    with tx(conn):
        if conn.execute("SELECT 1 FROM incidents WHERE position=?", (position,)).fetchone():
            return None
        eid = _next(conn, "event_id")
        body = builder(eid)
        conn.execute("INSERT INTO incidents(position,kind,detail,event_id) VALUES(?,?,?,?)",
                     (position, kind, detail, eid))
        conn.execute("INSERT INTO outbox(event_id,kind,job_id,payload) VALUES(?,?,?,?)",
                     (eid, "queue", None, json.dumps(body)))
        return body


# ---- publication states (Pi #5) -----------------------------------------
def staged_uncommitted(conn):
    return conn.execute("SELECT * FROM outbox WHERE local_committed=0 ORDER BY event_id").fetchall()


def mark_local_committed(conn, event_id):
    with tx(conn):
        conn.execute("UPDATE outbox SET local_committed=1 WHERE event_id=?", (event_id,))


def unacked(conn):
    return conn.execute("SELECT * FROM outbox WHERE local_committed=1 AND remote_acked=0 "
                        "ORDER BY event_id").fetchall()


def mark_remote_acked(conn, event_id):
    with tx(conn):
        conn.execute("UPDATE outbox SET remote_acked=1 WHERE event_id=?", (event_id,))


def alloc_runner_seq(conn):
    with tx(conn):
        return _next(conn, "runner_seq")
