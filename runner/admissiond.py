"""admissiond.py — the unprivileged admission poller (§3.4/§3.5/§3.6/§4).

Fetches jobs/queue from origin, admits by first-parent linear cursor, and drives
the privileged launch ONLY through `cmp-launch <registry-key> <queue-commit-sha>
<request-id>` (three tokens; no trust content crosses the boundary — the launcher
owns its own config, §4.2 / Pi #1). At-most-once is owned by the launcher's durable
sqlite lock (§3.5 / Pi #2); this poller never itself starts a unit and never
relaunches. Status/runner publication is crash-consistent (Pi #6): every event is
committed to the sqlite outbox BEFORE any git write, then published idempotently and
acked. FIFO: at most one admitted/running job at a time; traversal resumes only after
terminal reconciliation. Sticky fault cursor (Pi #5): valid commits before a fault are
processed and durably cursored; the offending position raises ONE deduped incident and
halts, so a later malformed commit can never hide earlier valid requests.
"""
import os, sys, json, time, subprocess, stat
import refs
import registry
import ledger
import cmp_launch

TERMINAL = {"succeeded", "failed", "rejected", "timeout", "indeterminate"}


class Poller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mirror = cfg["mirror"]
        self.origin = cfg.get("origin")
        self.qref = cfg.get("queue_ref", "refs/heads/jobs/queue")
        self.sref = cfg.get("status_ref", "refs/heads/jobs/status")
        self.rref = cfg.get("runner_ref", "refs/heads/jobs/runner")
        self.boxkey = cfg["box_key"]
        self.spool = cfg.get("spool")
        self.backend = cfg.get("backend", "systemd")
        self.db = cfg["ledger_db"]

    # -- event payload builders (frozen wire, §3.6) --
    def _job_ev(self, jid, state, reason, digest, qc, unit, principal, fpr, extra=None):
        def build(eid, seq):
            ev = {"schema": "cmp.job-status.v1", "event_id": "%06d" % eid, "job_id": jid,
                  "seq": seq, "state": state, "reason": reason, "request_digest": digest,
                  "queue_commit": qc, "unit": unit, "principal": principal,
                  "fingerprint": fpr, "observed_at": int(time.time())}
            if extra:
                ev.update(extra)
            return ev
        return build

    def _queue_ev(self, kind, detail, qc):
        def build(eid):
            return {"schema": "cmp.queue-event.v1", "event_id": "%06d" % eid, "kind": kind,
                    "queue_commit": qc, "ref_position": qc, "detail": detail,
                    "job_id": None, "request_digest": None, "observed_at": int(time.time())}
        return build

    # -- crash-safe publication: outbox -> git -> ack (Pi #6) --
    def publish_outbox(self, conn):
        pushed = False
        for row in ledger.unpublished(conn):
            payload = json.loads(row["payload"])
            refs.publish_event(self.mirror, self.sref, row["event_id"], payload, self.boxkey)
            ledger.mark_published(conn, row["event_id"])
            pushed = True
        if pushed and self.origin:
            try:
                refs.push_ref(self.mirror, self.origin, self.sref, force=False)
            except RuntimeError:
                pass  # events durable in mirror+outbox; origin push retried next poll

    # -- reconciliation: observe units, record terminals; NEVER relaunch (§3.5) --
    def reconcile(self, conn):
        for rec in ledger.intents(conn):
            show = cmp_launch.unit_show(self.cfg, rec["unit"])
            if show is None:
                self._terminal(conn, rec, "indeterminate", "launch_outcome_unknown")
            elif str(show.get("ActiveState")) == "active":
                if rec["state"] == "INTENT":
                    ledger.set_running(conn, rec["request_digest"])
                    ledger.enqueue_job_event(conn, rec["job_id"],
                        self._job_ev(rec["job_id"], "running", None, rec["request_digest"],
                                     rec["queue_commit"], rec["unit"], rec["principal"], rec["fingerprint"]))
                # else adopted — do NOT relaunch
            else:
                state, reason, mem, rt = cmp_launch.classify_terminal(show)
                self._terminal(conn, rec, state, reason, mem, rt)

    def _terminal(self, conn, rec, state, reason, mem=None, rt=None):
        ledger.record_terminal(conn, rec["request_digest"], state, reason,
            self._job_ev(rec["job_id"], state, reason, rec["request_digest"], rec["queue_commit"],
                         rec["unit"], rec["principal"], rec["fingerprint"],
                         extra={"memory_peak": mem, "RuntimeMaxSec": rt}),
            memory_peak=mem, runtime_max=rt)

    # -- privileged boundary: only three tokens cross it (§4.2, A3/A12) --
    def _invoke_launcher(self, job_key, sha, rid):
        env = {**os.environ, "CMP_LAUNCHER_CONFIG": self.cfg["launcher_config"]}
        if self.backend == "fake":
            env["CMP_FAKE_MODE"] = self.cfg.get("fake_mode", "active")
        here = os.path.dirname(os.path.abspath(__file__))
        r = subprocess.run([sys.executable, os.path.join(here, "cmp_launch.py"), job_key, sha, rid],
                           capture_output=True, env=env)
        try:
            return json.loads(r.stdout.decode() or "{}")
        except Exception:
            return {"ok": False, "error": r.stderr.decode()[:200]}

    # -- one poll cycle --
    def poll_once(self):
        conn = ledger.connect(self.db)
        self.publish_outbox(conn)                       # flush any pre-crash events first
        if self.origin:
            try:
                refs.fetch_queue(self.mirror, self.origin, self.qref)
            except RuntimeError as e:
                self._snapshot(conn, "FETCH-FAIL", str(e))
                self.publish_outbox(conn)
                return
        self.reconcile(conn)
        self.publish_outbox(conn)
        if ledger.in_flight(conn):                      # FIFO: resume only after terminal
            self._snapshot(conn, "OK")
            return
        cursor = ledger.meta_get(conn, "cursor")
        tip = refs.ref_sha(self.mirror, self.qref)
        if tip is None:
            self._snapshot(conn, "OK")
            return
        if cursor and not refs.is_ancestor(self.mirror, cursor, tip):
            self._incident(conn, tip, "QUEUE-TAMPER", "tip does not descend from last_accepted_commit")
            self.publish_outbox(conn)
            self._snapshot(conn, "QUEUE-HALT", "QUEUE-TAMPER")
            return
        rng = ("%s..%s" % (cursor, tip)) if cursor else tip
        shas = refs.git(self.mirror, "rev-list", "--first-parent", "--reverse", rng).stdout.decode().split()
        halted = None
        for sha in shas:
            kind, payload = self._classify(sha)
            if kind != "OK":
                self._incident(conn, sha, kind, payload)          # sticky, deduped
                halted = kind
                break
            if self._admit(conn, sha, payload) == "RUNNING":      # FIFO: one job, then stop
                break
        self.publish_outbox(conn)
        self._snapshot(conn, "QUEUE-HALT" if halted else "OK", halted)

    def _classify(self, sha):
        if len(refs.parents(self.mirror, sha)) > 1:
            return "QUEUE-NONLINEAR", "merge commit in queue history"
        adds = refs.added_paths(self.mirror, sha)
        if len(adds) != 1 or adds[0][0] != "A" or not adds[0][1].startswith("requests/") \
                or not adds[0][1].endswith(".json"):
            return "QUEUE-MALFORMED", "commit must add exactly one requests/<id>.json"
        rid = adds[0][1][len("requests/"):-len(".json")]
        try:
            principal, fpr = refs.verify_commit(self.mirror, sha, self.cfg["allowed_signers"])
        except ValueError as e:
            return "QUEUE-UNSIGNED", str(e)
        return "OK", {"rid": rid, "bytes": refs.read_blob(self.mirror, sha, adds[0][1]),
                      "principal": principal, "fingerprint": fpr}

    def _admit(self, conn, sha, r):
        digest = refs.request_digest(r["bytes"])
        jid = r["rid"]
        try:
            req = refs.validate_request(r["bytes"], jid)
            reg = registry.load(self.cfg["registry"])
            spec = registry.resolve(reg, req["job"], req["job_version"])
            registry.validate_params(spec, req.get("params", {}))
        except (refs.RequestError, registry.RegistryError) as e:
            self._reject(conn, jid, "schema_or_registry:%s" % e, digest, sha, r)
            ledger.meta_set(conn, "cursor", sha)
            return "REJECTED"
        prev = ledger.id_digest(conn, jid)               # AC11 pre-check (launcher re-enforces)
        if prev is not None and prev != digest:
            self._reject(conn, jid, "integrity_duplicate_id", digest, sha, r)
            ledger.meta_set(conn, "cursor", sha)
            return "REJECTED"
        out = self._invoke_launcher(req["job"], sha, jid)   # launcher owns the lock + launch
        if out.get("ok") and out.get("outcome") == "CREATED":
            ledger.enqueue_job_event(conn, jid, self._job_ev(jid, "admitted", None, digest, sha,
                                     out["unit"], r["principal"], r["fingerprint"]))
            ledger.meta_set(conn, "cursor", sha)
            self.reconcile(conn)                          # observe running/terminal now
            return "RUNNING"
        if out.get("outcome") in ("REPLAY_TERMINAL", "IN_FLIGHT"):
            ledger.meta_set(conn, "cursor", sha)
            return "REPLAY"
        if out.get("outcome") == "INTEGRITY":
            self._reject(conn, jid, "integrity_duplicate_id", digest, sha, r)
            ledger.meta_set(conn, "cursor", sha)
            return "REJECTED"
        self._incident(conn, sha, "QUEUE-UNSIGNED", out.get("error", "launcher rejected"))
        return "HALT"

    def _reject(self, conn, jid, reason, digest, qc, r):
        ledger.enqueue_job_event(conn, jid, self._job_ev(jid, "rejected", reason, digest, qc,
                                 None, r.get("principal"), r.get("fingerprint")))

    def _incident(self, conn, position, kind, detail):
        ledger.incident_once(conn, position, kind, detail, self._queue_ev(kind, detail, position))

    # -- runner snapshot (mutable, box-signed, §4.5); durable seq before git (Pi #6) --
    def _snapshot(self, conn, health, last_error=None):
        seq = ledger.alloc_runner_seq(conn)
        cur, progress = None, None
        for rec in ledger.intents(conn):
            if rec["state"] == "RUNNING":
                cur = rec["job_id"]
        if cur:
            progress = self._safe_progress(cur)
        snap = {"seq": seq, "observed_at": int(time.time()), "current_job": cur,
                "health": health, "last_error": last_error, "progress": progress}
        refs.runner_write(self.mirror, self.rref, snap, self.boxkey)
        if self.origin:
            try:
                refs.push_ref(self.mirror, self.origin, self.rref, force=True)
            except RuntimeError:
                pass
        return snap

    def _safe_progress(self, jid):
        """Relay (never author) the job's UNTRUSTED progress (§3.4/Pi #7): reject
        symlinks/non-regular files, bound the size, require the schema; isolate all
        exceptions (a hostile progress file cannot crash the poller)."""
        if not self.spool:
            return None
        p = os.path.join(self.spool, "cmpjob-%s.progress" % jid)
        try:
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                return None
            if st.st_size > 4096:
                return None
            d = json.loads(open(p, "rb").read().decode("utf-8"))
            if not isinstance(d, dict):
                return None
            c, t = d.get("counter"), d.get("ts")
            if not isinstance(c, int) or not isinstance(t, int) or c < 0:
                return None
            return {"counter": c, "ts": t}
        except Exception:
            return None


if __name__ == "__main__":
    Poller(json.load(open(sys.argv[1]))).poll_once()
