"""admissiond.py — the unprivileged admission poller (§3.5/§3.6/§4).

Pulls jobs/queue by first-parent linear cursor, admits only dispatcher-signed,
schema-valid, registry-known requests, enforces at-most-once launch via an atomic
root-owned ledger, and drives the privileged launch ONLY through the narrow
`cmp-launch <registry-key> <queue-commit-sha> <request-id>` boundary. It parses
attacker-controlled queue bytes but never itself starts a unit; the launcher
re-verifies everything (§4.2). Terminal state comes from the observed unit exit
(A7); an outcome that cannot be established is `indeterminate/launch_outcome_unknown`
and is NEVER auto-relaunched (§3.5).
"""
import os, sys, json, time, subprocess
import refs
import registry
import cmp_launch

TERMINAL = {"succeeded", "failed", "rejected", "timeout", "indeterminate"}


# ---- durable state (cursor, pins, counters) ------------------------------
class State:
    def __init__(self, path):
        self.path = path
        self.d = json.load(open(path)) if os.path.exists(path) else {
            "cursor": None, "event_id": 0, "jobseq": {}, "runner_seq": 0}

    def save(self):
        cmp_launch._atomic_json(self.path, self.d)

    def next_event_id(self):
        self.d["event_id"] += 1
        return self.d["event_id"]

    def next_jobseq(self, jid):
        n = self.d["jobseq"].get(jid, 0) + 1
        self.d["jobseq"][jid] = n
        return n


# ---- the atomic ledger (§3.5) --------------------------------------------
class Ledger:
    """root-owned dir, one record per request_digest. Two uniqueness keys:
    job_id -> request_digest, and request_digest itself. The O_EXCL create of the
    INTENT record is the launch lock."""
    def __init__(self, d):
        self.d = d
        os.makedirs(d, exist_ok=True)

    def _dg(self, digest):
        return os.path.join(self.d, "dg-%s.json" % digest)

    def _id(self, jid):
        return os.path.join(self.d, "id-%s" % jid)

    def id_digest(self, jid):
        p = self._id(jid)
        return open(p).read().strip() if os.path.exists(p) else None

    def get(self, digest):
        p = self._dg(digest)
        return json.load(open(p)) if os.path.exists(p) else None

    def put(self, rec):
        cmp_launch._atomic_json(self._dg(rec["digest"]), rec)

    def intent(self, digest, jid, unit, queue_commit):
        """Atomic O_EXCL create = the launch lock. Returns True iff we created it."""
        p = self._dg(digest)
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        rec = {"state": "INTENT", "id": jid, "unit": unit,
               "queue_commit": queue_commit, "digest": digest}
        with os.fdopen(fd, "w") as f:
            json.dump(rec, f)
            f.flush(); os.fsync(f.fileno())
        dfd = os.open(self.d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        cmp_launch._atomic_json(self._id(jid), digest)  # job_id -> digest binding
        return True

    def intents(self):
        for n in os.listdir(self.d):
            if n.startswith("dg-"):
                r = json.load(open(os.path.join(self.d, n)))
                if r["state"] == "INTENT" or r["state"] == "RUNNING":
                    yield r


class Poller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.st = State(cfg["state"])
        self.led = Ledger(cfg["ledger"])
        self.mirror = cfg["mirror"]
        self.qref = cfg.get("queue_ref", "refs/heads/jobs/queue")
        self.sref = cfg.get("status_ref", "refs/heads/jobs/status")
        self.rref = cfg.get("runner_ref", "refs/heads/jobs/runner")
        self.boxkey = cfg["box_key"]
        self.spool = cfg.get("spool")
        self.backend = cfg.get("backend", "fake")

    # -- status writers --
    def _job_event(self, jid, state, reason, digest, qc, unit, principal, fpr, extra=None):
        ev = {"schema": "cmp.job-status.v1", "event_id": "%06d" % self.st.next_event_id(),
              "job_id": jid, "seq": self.st.next_jobseq(jid), "state": state,
              "reason": reason, "request_digest": digest, "queue_commit": qc,
              "unit": unit, "principal": principal, "fingerprint": fpr,
              "observed_at": int(time.time())}
        if extra:
            ev.update(extra)
        refs.status_append(self.mirror, self.sref, ev, self.boxkey)
        self.st.save()
        return ev

    def _queue_event(self, kind, detail, qc):
        ev = {"schema": "cmp.queue-event.v1", "event_id": "%06d" % self.st.next_event_id(),
              "kind": kind, "queue_commit": qc, "ref_position": qc, "detail": detail,
              "job_id": None, "request_digest": None, "observed_at": int(time.time())}
        refs.status_append(self.mirror, self.sref, ev, self.boxkey)
        self.st.save()
        return ev

    # -- reconciliation (§3.5): runs before any launch, NEVER relaunches --
    def reconcile(self):
        for rec in list(self.led.intents()):
            show = cmp_launch.unit_show(self.spool, rec["unit"], self.backend)
            if show is None:
                # INTENT/RUNNING present, no unit, no terminal -> truthful indeterminate
                self._terminal(rec, "indeterminate", "launch_outcome_unknown")
            else:
                active = str(show.get("ActiveState")) == "active"
                if active:
                    if rec["state"] == "INTENT":
                        rec["state"] = "RUNNING"
                        self.led.put(rec)
                        self._job_event(rec["id"], "running", None, rec["digest"],
                                        rec["queue_commit"], rec["unit"],
                                        rec.get("principal"), rec.get("fingerprint"))
                    # else already adopted; do NOT relaunch
                else:
                    self._terminal_from_show(rec, show)

    def _terminal(self, rec, state, reason, extra=None):
        rec["state"] = state.upper()
        self.led.put(rec)
        self._job_event(rec["id"], state, reason, rec["digest"], rec["queue_commit"],
                        rec["unit"], rec.get("principal"), rec.get("fingerprint"), extra)

    def _terminal_from_show(self, rec, show):
        res = show.get("Result", "")
        extra = {"memory_peak": show.get("memory_peak"),
                 "RuntimeMaxSec": show.get("RuntimeMaxSec")}
        if res == "success" and int(show.get("ExecMainStatus", 0)) == 0:
            self._terminal(rec, "succeeded", None, extra)
        elif res in ("oom-kill",) or show.get("systemd_result") == "CONSTRAINT_MEMCG":
            self._terminal(rec, "failed", "CONSTRAINT_MEMCG", extra)
        elif res in ("timeout",):
            self._terminal(rec, "timeout", "RuntimeMaxSec", extra)
        else:
            self._terminal(rec, "failed", "exit_%s" % show.get("ExecMainStatus"), extra)

    # -- the privileged boundary: only three tokens cross it (§4.2, A3) --
    def _invoke_launcher(self, job_key, sha, rid):
        env = {**os.environ, "CMP_MIRROR": self.mirror,
               "CMP_ALLOWED_SIGNERS": self.cfg["allowed_signers"],
               "CMP_REGISTRY": self.cfg["registry"], "CMP_QUEUE_REF": self.qref,
               "CMP_BACKEND": self.backend, "CMP_SPOOL": self.spool or "",
               "CMP_FAKE_MODE": self.cfg.get("fake_mode", "active")}
        here = os.path.dirname(os.path.abspath(__file__))
        r = subprocess.run([sys.executable, os.path.join(here, "cmp_launch.py"),
                            job_key, sha, rid], capture_output=True, env=env)
        try:
            out = json.loads(r.stdout.decode() or "{}")
        except Exception:
            out = {"ok": False, "error": r.stderr.decode()[:200]}
        return r.returncode == 0, out

    def poll_once(self):
        self.reconcile()
        try:
            reqs = refs.read_queue(self.mirror, self.qref, self.st.d["cursor"], self.cfg["allowed_signers"])
        except refs.QueueError as e:
            self._queue_event(e.kind, e.detail, e.sha)
            self._runner_snapshot(health="QUEUE-HALT", last_error=e.kind)
            return
        except ValueError as e:  # verify_commit unsigned/wrong-key inside traversal
            self._queue_event("QUEUE-UNSIGNED", str(e), None)
            self._runner_snapshot(health="QUEUE-HALT", last_error="QUEUE-UNSIGNED")
            return
        for r in reqs:
            self._admit(r)
            self.st.d["cursor"] = r["sha"]        # advance cursor + last_accepted_commit
            self.st.save()
        self._runner_snapshot(health="OK")

    def _admit(self, r):
        digest = refs.request_digest(r["bytes"])
        qc, jid = r["sha"], r["id"]
        # schema + id grammar
        try:
            req = refs.validate_request(r["bytes"], jid)
        except refs.RequestError as e:
            return self._reject(jid, "schema_invalid", digest, qc, r)
        # registry resolve + params
        try:
            reg = registry.load(self.cfg["registry"])
            spec = registry.resolve(reg, req["job"], req["job_version"])
            registry.validate_params(spec, req.get("params", {}))
        except registry.RegistryError as e:
            return self._reject(jid, "registry:%s" % e, digest, qc, r)
        # AC11: duplicate id, different digest -> integrity reject (not replay)
        prev = self.led.id_digest(jid)
        if prev is not None and prev != digest:
            return self._reject(jid, "integrity_duplicate_id", digest, qc, r)
        # replay / already-launched guard
        existing = self.led.get(digest)
        if existing is not None:
            if existing["state"] in {s.upper() for s in TERMINAL}:
                return  # replay rejected (terminal already recorded); no relaunch
            return       # INTENT/RUNNING in flight; reconciliation owns it
        # at-most-once: atomic INTENT (launch lock) + unit-name lock in launcher
        unit = "cmpjob-%s" % jid
        if not self.led.intent(digest, jid, unit, qc):
            return  # lost the create race
        self._job_event(jid, "admitted", None, digest, qc, unit, r["principal"], r["fingerprint"])
        ok, out = self._invoke_launcher(req["job"], qc, jid)
        rec = self.led.get(digest)
        rec["principal"], rec["fingerprint"] = r["principal"], r["fingerprint"]
        self.led.put(rec)
        if not ok:
            self._terminal(rec, "rejected", "launcher:%s" % out.get("error", "?"))
            return
        # observe outcome (idempotent with reconcile)
        self.reconcile()

    def _reject(self, jid, reason, digest, qc, r):
        self._job_event(jid, "rejected", reason, digest, qc, None,
                        r.get("principal"), r.get("fingerprint"))

    def _runner_snapshot(self, health, last_error=None):
        self.st.d["runner_seq"] += 1
        cur, progress = None, None
        for rec in self.led.intents():
            if rec["state"] == "RUNNING":
                cur = rec["id"]
        if cur and self.spool:  # relay (never author) the job's untrusted progress (§3.4)
            pp = os.path.join(self.spool, "cmpjob-%s.progress" % cur)
            if os.path.exists(pp):
                progress = json.load(open(pp))
        snap = {"seq": self.st.d["runner_seq"], "observed_at": int(time.time()),
                "current_job": cur, "health": health, "last_error": last_error,
                "progress": progress}
        refs.runner_write(self.mirror, self.rref, snap, self.boxkey)
        self.st.save()
        return snap


if __name__ == "__main__":
    cfg = json.load(open(sys.argv[1]))
    Poller(cfg).poll_once()
