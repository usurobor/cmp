"""ledger.py — one root-owned transactional admission ledger + outbox (sqlite3).

box-job-admission v0.5.1 @ ce91739, §3.5 (at-most-once). stdlib sqlite3 only.

This is the single source of truth at the PRIVILEGED boundary (Pi impl-audit #2/#6).
It atomically binds {job_id, request_digest, launch intent, unit identity,
terminal/outbox state} so that:
  - the launcher itself takes the durable at-most-once lock (a compromised poller
    calling the launcher repeatedly for the same signed request is refused by the
    UNIQUE(request_digest) INSERT, which survives systemd `--collect`);
  - job_id -> request_digest is a UNIQUE binding (a reused id with a different
    digest is an integrity violation caught inside the same transaction, AC11);
  - status/runner publication is crash-consistent: the terminal ledger state, its
    event_id (durably allocated, never reused after a crash), per-job seq and the
    exact event payload are committed together in the outbox BEFORE any git write,
    then published idempotently and acked.
"""
import sqlite3, time, json
from contextlib import contextmanager

TERMINAL = {"SUCCEEDED", "FAILED", "REJECTED", "TIMEOUT", "INDETERMINATE"}

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
  event_id  INTEGER PRIMARY KEY,
  kind      TEXT,
  job_id    TEXT,
  payload   TEXT,
  published INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS incidents(
  position TEXT PRIMARY KEY, kind TEXT, detail TEXT, event_id INTEGER
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def meta_get(conn, key, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def meta_set(conn, key, value):
    with tx(conn):
        conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


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
    # first allocation returns 1 (event_id 000001, per-job seq 1, ...)
    conn.execute("INSERT INTO counters(name,value) VALUES(?,1) "
                 "ON CONFLICT(name) DO UPDATE SET value=value+1", (name,))
    return conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()["value"]


def alloc_event_id(conn):
    return _next(conn, "event_id")


def alloc_runner_seq(conn):
    with tx(conn):
        return _next(conn, "runner_seq")


def next_jobseq(conn, job_id):
    conn.execute("INSERT INTO jobseq(job_id,seq) VALUES(?,1) "
                 "ON CONFLICT(job_id) DO UPDATE SET seq=seq+1", (job_id,))
    return conn.execute("SELECT seq FROM jobseq WHERE job_id=?", (job_id,)).fetchone()["seq"]


def get(conn, digest):
    return conn.execute("SELECT * FROM ledger WHERE request_digest=?", (digest,)).fetchone()


def id_digest(conn, job_id):
    r = conn.execute("SELECT request_digest FROM ledger WHERE job_id=?", (job_id,)).fetchone()
    return r["request_digest"] if r else None


def intents(conn):
    return conn.execute("SELECT * FROM ledger WHERE state IN ('INTENT','RUNNING')").fetchall()


def try_intent(conn, digest, job_id, unit, queue_commit, principal, fingerprint):
    """The durable at-most-once launch lock (§3.5). One transaction:
    returns 'REPLAY_TERMINAL' | 'IN_FLIGHT' | 'INTEGRITY' | 'CREATED'.
    principal/fingerprint are recorded WITH the intent (before launch), so a crash
    after launch cannot lose them (Pi #6)."""
    with tx(conn):
        row = conn.execute("SELECT state FROM ledger WHERE request_digest=?", (digest,)).fetchone()
        if row:
            return "REPLAY_TERMINAL" if row["state"] in TERMINAL else "IN_FLIGHT"
        prev = conn.execute("SELECT request_digest FROM ledger WHERE job_id=?", (job_id,)).fetchone()
        if prev and prev["request_digest"] != digest:
            return "INTEGRITY"
        now = int(time.time())
        try:
            conn.execute("INSERT INTO ledger(request_digest,job_id,unit,queue_commit,"
                         "principal,fingerprint,state,created_at,updated_at) "
                         "VALUES(?,?,?,?,?,?, 'INTENT', ?, ?)",
                         (digest, job_id, unit, queue_commit, principal, fingerprint, now, now))
        except sqlite3.IntegrityError:
            return "INTEGRITY"
        return "CREATED"


def bind_input(conn, digest, input_path, input_digest):
    with tx(conn):
        conn.execute("UPDATE ledger SET input_path=?, input_digest=?, updated_at=? "
                     "WHERE request_digest=?", (input_path, input_digest, int(time.time()), digest))


def set_running(conn, digest):
    with tx(conn):
        conn.execute("UPDATE ledger SET state='RUNNING', updated_at=? WHERE request_digest=? "
                     "AND state='INTENT'", (int(time.time()), digest))


def record_terminal(conn, digest, state, reason, event_builder, memory_peak=None, runtime_max=None):
    """Commit terminal ledger state + its status event ATOMICALLY into the outbox
    (Pi #6: terminal is never persisted without its event, and the event_id is
    allocated durably so a crash cannot cause reuse). Idempotent: if already
    terminal, returns None. event_builder(event_id, seq) -> dict payload."""
    st = state.upper()
    with tx(conn):
        row = conn.execute("SELECT state,job_id FROM ledger WHERE request_digest=?", (digest,)).fetchone()
        if row is None or row["state"] in TERMINAL:
            return None
        eid = _next(conn, "event_id")
        # jobseq inline (same tx)
        conn.execute("INSERT INTO jobseq(job_id,seq) VALUES(?,1) "
                     "ON CONFLICT(job_id) DO UPDATE SET seq=seq+1", (row["job_id"],))
        seq = conn.execute("SELECT seq FROM jobseq WHERE job_id=?", (row["job_id"],)).fetchone()["seq"]
        payload = event_builder(eid, seq)
        conn.execute("UPDATE ledger SET state=?, memory_peak=?, runtime_max=?, updated_at=? "
                     "WHERE request_digest=?", (st, memory_peak, runtime_max, int(time.time()), digest))
        conn.execute("INSERT INTO outbox(event_id,kind,job_id,payload,published) VALUES(?,?,?,?,0)",
                     (eid, "job", row["job_id"], json.dumps(payload)))
        return payload


def enqueue_job_event(conn, job_id, event_builder):
    """Non-terminal job event (admitted/running) staged to outbox atomically."""
    with tx(conn):
        eid = _next(conn, "event_id")
        conn.execute("INSERT INTO jobseq(job_id,seq) VALUES(?,1) "
                     "ON CONFLICT(job_id) DO UPDATE SET seq=seq+1", (job_id,))
        seq = conn.execute("SELECT seq FROM jobseq WHERE job_id=?", (job_id,)).fetchone()["seq"]
        payload = event_builder(eid, seq)
        conn.execute("INSERT INTO outbox(event_id,kind,job_id,payload,published) VALUES(?,?,?,?,0)",
                     (eid, "job", job_id, json.dumps(payload)))
        return payload


def incident_once(conn, position, kind, detail, event_builder):
    """Sticky fault cursor (Pi #5): record ONE incident per offending position;
    if this position was already recorded, return None (dedupe — do not re-emit the
    same queue-error every poll)."""
    with tx(conn):
        if conn.execute("SELECT 1 FROM incidents WHERE position=?", (position,)).fetchone():
            return None
        eid = _next(conn, "event_id")
        payload = event_builder(eid)
        conn.execute("INSERT INTO incidents(position,kind,detail,event_id) VALUES(?,?,?,?)",
                     (position, kind, detail, eid))
        conn.execute("INSERT INTO outbox(event_id,kind,job_id,payload,published) VALUES(?,?,?,?,0)",
                     (eid, "queue", None, json.dumps(payload)))
        return payload


def unpublished(conn):
    return conn.execute("SELECT * FROM outbox WHERE published=0 ORDER BY event_id").fetchall()


def mark_published(conn, event_id):
    with tx(conn):
        conn.execute("UPDATE outbox SET published=1 WHERE event_id=?", (event_id,))


def in_flight(conn):
    """FIFO gate: is a non-terminal job currently admitted/running?"""
    return conn.execute("SELECT 1 FROM ledger WHERE state IN ('INTENT','RUNNING') LIMIT 1").fetchone() is not None
