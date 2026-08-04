"""admissiond.py — the UNPRIVILEGED admission poller (§3.4/§3.6/§4, Pi re-audit #1).

This process holds NO authority. Its config references neither the ledger DB, nor
the box private key, nor the backend/spool/runtime — it has no code path to any of
them. It does only untrusted work: fetch jobs/queue from origin, parse
attacker-controlled queue bytes to order candidates, and PUSH box-signed objects the
privileged component already produced. Everything authoritative (the at-most-once
lock, launch, unit observation, terminal transitions, box signing) is reached ONLY
through cmp_admitd verbs. The poll cursor here is a NON-authoritative cache; the
ledger is the real dedup. Because the poller has no private key, it cannot forge the
box-signed status/runner evidence external readers trust.
"""
import os, sys, json, subprocess
import refs


class Poller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mirror = cfg["mirror"]
        self.origin = cfg.get("origin")
        self.qref = cfg.get("queue_ref", "refs/heads/jobs/queue")
        self.admitd = cfg["admitd"]                     # path to the privileged verb entry
        self.cursor_cache = cfg["cursor_cache"]

    # -- the ONLY channel to authority: narrow verbs, scrubbed env --
    def _verb(self, *args):
        env = {"PATH": os.environ.get("PATH", ""),
               "CMP_ADMIT_CONFIG": os.environ.get("CMP_ADMIT_CONFIG", "")}  # fixture pointer only
        r = subprocess.run([sys.executable, self.admitd, *[str(a) for a in args]],
                           capture_output=True, env=env)
        try:
            return json.loads(r.stdout.decode() or "{}")
        except Exception:
            return {"ok": False, "error": r.stderr.decode()[:200]}

    def _cursor(self):
        try:
            return open(self.cursor_cache).read().strip() or None
        except OSError:
            return None

    def _set_cursor(self, sha):
        open(self.cursor_cache, "w").write(sha)

    def _parse(self, sha):
        """Untrusted parse to produce (job_key, id) args for admit; admit re-derives
        and re-verifies everything authoritatively."""
        adds = refs.added_paths(self.mirror, sha)
        if len(adds) != 1 or not adds[0][1].startswith("requests/") or not adds[0][1].endswith(".json"):
            return "?", "?"
        rid = adds[0][1][len("requests/"):-len(".json")]
        try:
            job = json.loads(refs.read_blob(self.mirror, sha, adds[0][1])).get("job", "?")
            job = job if isinstance(job, str) and job else "?"
        except Exception:
            job = "?"
        return job, rid

    def _publish(self):
        """Push box-signed objects to origin and ack only after verifying the remote
        actually has them (Pi #5 crash-consistency). No signing here."""
        pub = self._verb("next-publishable")
        for ev in pub.get("events", []):
            try:
                refs.push_ref(self.mirror, self.origin, ev["ref"], force=False)
                if refs.remote_has_event(self.origin, ev["ref"], "%06d" % ev["event_id"]):
                    self._verb("ack-published", ev["event_id"])
            except RuntimeError:
                pass                                     # remote_acked stays 0; retried next poll
        if pub.get("runner_seq"):
            try:
                refs.push_ref(self.mirror, self.origin, pub["runner_ref"], force=True)
                self._verb("ack-runner", pub["runner_seq"])
            except RuntimeError:
                pass

    def poll_once(self):
        if self.origin:
            try:
                refs.fetch_queue(self.mirror, self.origin, self.qref)
            except RuntimeError as e:
                self._verb("observe", "--health", "FETCH-FAIL")
                self._publish()
                return
        obs = self._verb("observe")
        self._publish()
        if obs.get("in_flight"):                          # FIFO: resume only after terminal
            return
        tip = refs.ref_sha(self.mirror, self.qref)
        if tip is None:
            return
        cursor = self._cursor()
        if cursor and not refs.is_ancestor(self.mirror, cursor, tip):
            self._verb("incident", "QUEUE-TAMPER", tip, "tip does not descend from cursor")
            self._publish()
            return
        rng = ("%s..%s" % (cursor, tip)) if cursor else tip
        shas = refs.git(self.mirror, "rev-list", "--first-parent", "--reverse", rng).stdout.decode().split()
        for sha in shas:
            job_key, rid = self._parse(sha)
            out = self._verb("admit", job_key, sha, rid)
            oc = out.get("outcome")
            if oc == "CREATED":
                self._set_cursor(sha)
                break                                    # FIFO: one job per poll
            if oc in ("REJECTED", "REPLAY", "INTEGRITY"):
                self._set_cursor(sha)
                continue
            # HALT (infrastructure/queue-level): sticky — do not advance past the fault
            break
        self._verb("observe")                            # observe the just-launched job
        self._publish()


if __name__ == "__main__":
    Poller(json.load(open(sys.argv[1]))).poll_once()
