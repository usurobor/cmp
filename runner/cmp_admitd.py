"""cmp_admitd.py — the PRIVILEGED admission component (§3.5/§4.2, Pi re-audit #1/#2).

This is the ONLY component that holds authority. It exclusively owns:
  - the sqlite ledger (the authoritative at-most-once launch lock),
  - the box PRIVATE signing key (all status/runner objects are signed here),
  - launch + unit OBSERVATION (via the trusted root-owned backend identity),
  - the outbox transitions (staged -> local_committed -> remote_acked).

It self-configures from a compiled-in root-owned path (DEFAULT_CONFIG); a test
config is reachable only via CMP_ADMIT_CONFIG WHEN the production path is absent —
production never consults the environment. The unprivileged poller has NO path to
the ledger DB or the private key and cannot forge box signatures; it drives this
component through a narrow verb API only:

    admit <job_key> <queue_sha> <request_id>   -> {outcome: CREATED|REJECTED|REPLAY|INTEGRITY|HALT, ...}
    incident <kind> <position> <detail>        -> record a deduped queue-level fault
    observe [--health CODE]                    -> reconcile units + sign staged status/runner locally
    next-publishable                           -> events/runner needing an origin push
    ack-published <event_id>                   -> mark remote_acked (poller pushed + verified)
    ack-runner <seq>                           -> mark the runner snapshot remote-acked
"""
import os, sys, json, time
import refs
import registry
import ledger
import cmp_launch

DEFAULT_CONFIG = "/etc/cmp-jobs/admit.json"
REQ = ("mirror", "origin", "ledger_db", "box_key", "box_signers", "allowed_signers",
       "registry", "runtime_dir", "backend", "spool", "queue_ref", "status_ref", "runner_ref")


def _config_path():
    if os.path.exists(DEFAULT_CONFIG):
        return DEFAULT_CONFIG                      # production: compiled-in, root-owned
    p = os.environ.get("CMP_ADMIT_CONFIG")         # fixture stand-in ONLY (no /etc to write)
    if not p:
        raise cmp_launch.LaunchError("no admit config")
    return p


class Admitd:
    def __init__(self, cfg):
        for k in REQ:
            if k not in cfg:
                raise cmp_launch.LaunchError("admit config missing %s" % k)
        self.c = cfg
        self.conn = ledger.connect(cfg["ledger_db"])

    @classmethod
    def load(cls):
        return cls(json.load(open(_config_path())))

    # -- event builders (frozen wire) --
    def _job(self, jid, state, reason, digest, qc, unit, principal, fpr, extra=None):
        def b(eid, seq):
            ev = {"schema": "cmp.job-status.v1", "event_id": "%06d" % eid, "job_id": jid, "seq": seq,
                  "state": state, "reason": reason, "request_digest": digest, "queue_commit": qc,
                  "unit": unit, "principal": principal, "fingerprint": fpr, "observed_at": int(time.time())}
            if extra:
                ev.update(extra)
            return ev
        return b

    def _queue(self, kind, detail, qc):
        return lambda eid: {"schema": "cmp.queue-event.v1", "event_id": "%06d" % eid, "kind": kind,
                            "queue_commit": qc, "ref_position": qc, "detail": detail, "job_id": None,
                            "request_digest": None, "observed_at": int(time.time())}

    # -- verb: admit one queue commit (authoritative) --
    def admit(self, job_key, sha, rid):
        c, conn = self.c, self.conn
        mirror, ref = c["mirror"], c["queue_ref"]
        tip = refs.ref_sha(mirror, ref)
        if tip is None or (tip != sha and not refs.is_ancestor(mirror, sha, tip)):
            return self._halt(sha, "QUEUE-TAMPER", "commit not on queue ref")
        if len(refs.parents(mirror, sha)) > 1:
            return self._halt(sha, "QUEUE-NONLINEAR", "merge commit in queue history")
        adds = refs.added_paths(mirror, sha)
        want = "requests/%s.json" % rid
        if len(adds) != 1 or adds[0][0] != "A" or not adds[0][1].startswith("requests/") \
                or not adds[0][1].endswith(".json"):
            return self._halt(sha, "QUEUE-MALFORMED", "commit must add exactly one requests/<id>.json")
        if adds[0][1] != want:
            return self._halt(sha, "QUEUE-MALFORMED", "added path != requested id")
        raw = refs.read_blob(mirror, sha, want)
        try:
            principal, fpr = refs.verify_commit(mirror, sha, c["allowed_signers"])
        except ValueError as e:
            return self._halt(sha, "QUEUE-UNSIGNED", str(e))          # signature ONLY
        digest = refs.request_digest(raw)
        try:
            req = refs.validate_request(raw, rid)
            if req["job"] != job_key:
                raise refs.RequestError("job %r != launch key %r" % (req["job"], job_key))
            reg = registry.load(c["registry"])
            spec = registry.resolve(reg, req["job"], req["job_version"])
            registry.verify_exec(spec)
            registry.validate_params(spec, req.get("params", {}))
        except (refs.RequestError, registry.RegistryError) as e:
            self._reject_event(rid, "schema_or_registry:%s" % e, digest, sha, principal, fpr)
            return {"outcome": "REJECTED", "reason": str(e)}          # known request problem
        unit = "cmpjob-%s" % rid
        res = ledger.try_intent(conn, digest, rid, unit, sha, principal, fpr)
        if res == "REPLAY_TERMINAL":
            return {"outcome": "REPLAY", "digest": digest}
        if res == "IN_FLIGHT":
            return {"outcome": "REPLAY", "reason": "in_flight", "digest": digest}
        if res == "INTEGRITY":
            self._reject_event(rid, "integrity_duplicate_id", digest, sha, principal, fpr)
            return {"outcome": "INTEGRITY", "digest": digest}
        return self._create(spec, req, raw, digest, rid, sha, unit, principal, fpr)

    def _create(self, spec, req, raw, digest, rid, sha, unit, principal, fpr):
        conn, c = self.conn, self.c
        # PREPARED: deliver the verified request as a job-readable immutable file
        try:
            req_path, note = cmp_launch.deliver_request(c["runtime_dir"], unit, raw, spec["user"])
        except OSError as e:
            return self._prelaunch_reject(digest, rid, sha, unit, principal, fpr, "prep_failed:%s" % e, None)
        idig = refs.request_digest(raw)
        ledger.set_phase(conn, digest, "PREPARED", req_path, idig)
        # backend selection fails closed BEFORE the start boundary
        try:
            backend = cmp_launch.make_backend(c["backend"], c["spool"], c.get("test_only"))
        except cmp_launch.LaunchError as e:
            return self._prelaunch_reject(digest, rid, sha, unit, principal, fpr, "backend_unavailable:%s" % e, req_path)
        ledger.set_phase(conn, digest, "STARTING")                    # the start-call boundary
        try:
            backend.start(unit, spec, req_path, c.get("fake_mode", "active"))
        except cmp_launch.LaunchError as e:
            return self._prelaunch_reject(digest, rid, sha, unit, principal, fpr, "start_refused:%s" % e, req_path)
        ledger.set_phase(conn, digest, "RUNNING")                     # after start returned
        ledger.enqueue_job_event(conn, rid, self._job(rid, "admitted", None, digest, sha, unit, principal, fpr))
        return {"outcome": "CREATED", "unit": unit, "request_digest": digest, "input_digest": idig,
                "principal": principal, "fingerprint": fpr, "delivery": note}

    def _prelaunch_reject(self, digest, rid, sha, unit, principal, fpr, reason, req_path):
        cmp_launch.cleanup_request(req_path)
        ledger.record_terminal(self.conn, digest, "rejected", reason,
                               self._job(rid, "rejected", reason, digest, sha, unit, principal, fpr))
        return {"outcome": "REJECTED", "reason": reason}

    def _reject_event(self, rid, reason, digest, sha, principal, fpr):
        ledger.enqueue_job_event(self.conn, rid,
                                 self._job(rid, "rejected", reason, digest, sha, None, principal, fpr))

    def _halt(self, sha, kind, detail):
        ledger.incident_once(self.conn, sha, kind, detail, self._queue(kind, detail, sha))
        return {"outcome": "HALT", "kind": kind, "detail": detail}

    # -- verb: record a poller-detected queue-level fault (box-signed + published) --
    def incident(self, kind, position, detail):
        ledger.incident_once(self.conn, position, kind, detail, self._queue(kind, detail, position))
        self._publish_local()                          # box-sign the staged queue-event into the mirror
        self._snapshot("QUEUE-HALT", kind)
        return {"outcome": "HALT", "kind": kind}

    # -- verb: reconcile units + sign staged status/runner locally (NEVER relaunch) --
    def observe(self, health="OK", last_error=None):
        conn, c = self.conn, self.c
        for rec in ledger.intents(conn):
            ph = rec["state"]
            if ph in ("RESERVED", "PREPARED"):
                cmp_launch.cleanup_request(rec["input_path"])
                self._term(rec, "rejected", "crashed_before_start")   # pre-launch -> REJECTED
            elif ph == "STARTING":
                self._term(rec, "indeterminate", "launch_outcome_unknown")  # at boundary -> unknown
            else:  # RUNNING
                show = cmp_launch.unit_show(c["backend"], c["spool"], rec["unit"])
                if show is None:
                    self._term(rec, "indeterminate", "launch_outcome_unknown")
                elif str(show.get("ActiveState")) == "active":
                    if ledger.meta_get(conn, "run_evt:%s" % rec["request_digest"]) is None:
                        ledger.enqueue_job_event(conn, rec["job_id"],
                            self._job(rec["job_id"], "running", None, rec["request_digest"],
                                      rec["queue_commit"], rec["unit"], rec["principal"], rec["fingerprint"]))
                        ledger.meta_set(conn, "run_evt:%s" % rec["request_digest"], "1")
                else:
                    st, reason, mem, rt = cmp_launch.classify_terminal(show)
                    self._term(rec, st, reason, mem, rt)
        self._publish_local()
        self._snapshot(health, last_error)
        return {"in_flight": ledger.in_flight(conn), "health": health}

    def _term(self, rec, state, reason, mem=None, rt=None):
        if state in ("succeeded", "failed", "timeout"):
            cmp_launch.cleanup_request(rec["input_path"])
        ledger.record_terminal(self.conn, rec["request_digest"], state, reason,
            self._job(rec["job_id"], state, reason, rec["request_digest"], rec["queue_commit"],
                      rec["unit"], rec["principal"], rec["fingerprint"],
                      extra={"memory_peak": mem, "RuntimeMaxSec": rt}), memory_peak=mem, runtime_max=rt)

    # -- local box-signing of staged events + runner snapshot (Pi #5 bootstrap) --
    def _bootstrap(self):
        c = self.c
        if not c.get("origin"):
            return
        for ref in (c["status_ref"], c["runner_ref"]):
            try:
                r = refs.git(c["mirror"], "fetch", c["origin"], "%s:refs/remotes/origin_boot/%s" % (ref, ref), check=False)
                if r.returncode != 0:
                    continue
                rtip = refs.ref_sha(c["mirror"], "refs/remotes/origin_boot/%s" % ref)
                ltip = refs.ref_sha(c["mirror"], ref)
                if rtip and ltip is None:
                    refs.verify_commit(c["mirror"], rtip, c["box_signers"])   # trust remote history
                    refs.git(c["mirror"], "update-ref", ref, rtip)
                elif rtip and ltip and rtip != ltip and refs.is_ancestor(c["mirror"], ltip, rtip):
                    refs.verify_commit(c["mirror"], rtip, c["box_signers"])
                    refs.git(c["mirror"], "update-ref", ref, rtip)
            except (RuntimeError, ValueError):
                continue

    def _publish_local(self):
        c, conn = self.c, self.conn
        self._bootstrap()
        for row in ledger.staged_uncommitted(conn):
            payload = json.loads(row["payload"])
            refs.publish_event(c["mirror"], c["status_ref"], row["event_id"], payload, c["box_key"])
            ledger.mark_local_committed(conn, row["event_id"])

    def _snapshot(self, health, last_error):
        c, conn = self.c, self.conn
        seq = ledger.alloc_runner_seq(conn)
        cur, progress = None, None
        for rec in ledger.intents(conn):
            if rec["state"] == "RUNNING":
                cur = rec["job_id"]
        if cur:
            progress = self._safe_progress(cur)
        snap = {"seq": seq, "observed_at": int(time.time()), "current_job": cur,
                "health": health, "last_error": last_error, "progress": progress}
        refs.runner_write(c["mirror"], c["runner_ref"], snap, c["box_key"])
        ledger.meta_set(conn, "runner_needs_push", seq)

    def _safe_progress(self, jid):
        import stat
        p = os.path.join(self.c["spool"], "cmpjob-%s.progress" % jid)
        try:
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size > 4096:
                return None
            d = json.loads(open(p, "rb").read().decode("utf-8"))
            if not isinstance(d, dict):
                return None
            cnt, ts = d.get("counter"), d.get("ts")
            return {"counter": cnt, "ts": ts} if isinstance(cnt, int) and isinstance(ts, int) and cnt >= 0 else None
        except Exception:
            return None

    # -- publication verbs (poller pushes; privileged tracks remote-ack) --
    def next_publishable(self):
        conn, c = self.conn, self.c
        evs = [{"event_id": r["event_id"], "ref": c["status_ref"]} for r in ledger.unacked(conn)]
        runner = ledger.meta_get(conn, "runner_needs_push")
        return {"events": evs, "runner_ref": c["runner_ref"], "runner_seq": int(runner) if runner else None}

    def ack_published(self, event_id):
        ledger.mark_remote_acked(self.conn, int(event_id))
        return {"ok": True}

    def ack_runner(self, seq):
        cur = ledger.meta_get(self.conn, "runner_needs_push")
        if cur is not None and int(seq) >= int(cur):
            ledger.meta_set(self.conn, "runner_needs_push", "")
        return {"ok": True}


def main(argv):
    try:
        a = Admitd.load()
    except cmp_launch.LaunchError as e:
        print(json.dumps({"ok": False, "error": str(e)})); return 2
    if len(argv) < 2:
        print(json.dumps({"ok": False, "error": "no verb"})); return 2
    verb, rest = argv[1], argv[2:]
    try:
        if verb == "admit":
            out = a.admit(rest[0], rest[1], rest[2])
        elif verb == "incident":
            out = a.incident(rest[0], rest[1], rest[2] if len(rest) > 2 else "")
        elif verb == "observe":
            health = "OK"; le = None
            if "--health" in rest:
                health = rest[rest.index("--health") + 1]; le = health
            out = a.observe(health, le)
        elif verb == "next-publishable":
            out = a.next_publishable()
        elif verb == "ack-published":
            out = a.ack_published(rest[0])
        elif verb == "ack-runner":
            out = a.ack_runner(rest[0])
        else:
            print(json.dumps({"ok": False, "error": "unknown verb %s" % verb})); return 2
    except (cmp_launch.LaunchError, refs.RequestError, registry.RegistryError, RuntimeError) as e:
        print(json.dumps({"ok": False, "error": str(e)})); return 1
    out.setdefault("ok", True)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
