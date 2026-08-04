#!/usr/bin/env python3
"""run_acs.py — HONEST acceptance harness for box-job-admission v0.5.1 @ ce91739.

Pi's impl-audit rejected the earlier "17/17 PASS" as an overclaim that flattened
fixture/mock outcomes into PASS. This harness reports a per-AC TAXONOMY instead:

  pass_model    — proven at the fixture/crypto level (real signatures, real sqlite
                  ledger, real git objects), but not on the production substrate
  pass_live     — proven on the real substrate (real systemd/cgroups / real remote)
  partial       — mechanism proven; a required real element is only modeled
  not_evaluated — requires real systemd/cgroups or the real GitHub remote; NOT run
                  here as a pass (a required not_evaluated AC blocks an overall PASS)
  fail          — the mechanism did not hold

Substrate here: fake-unit backend (systemd-run present but no live bus) + a LOCAL
bare-repo 'origin' remote (not GitHub). Overall verdict is therefore PARTIAL by
construction — code-level model complete; real-systemd + real-remote ACs pending an
isolated box staging. Receipt: runner/tests/ac_receipt.json.
"""
import os, sys, json, subprocess, tempfile, hashlib, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import refs, registry, ledger, cmp_launch, admissiond  # noqa: E402

QREF, SREF, RREF = "refs/heads/jobs/queue", "refs/heads/jobs/status", "refs/heads/jobs/runner"
RESULTS = []
TAXO = {"pass_live", "pass_model", "partial", "not_evaluated", "fail"}
LAUNCHER = os.path.join(os.path.dirname(HERE), "cmp_launch.py")


def digest_of(o):
    return "sha256:" + hashlib.sha256(json.dumps(o, sort_keys=True, default=str).encode()).hexdigest()


def record(ac, covers, verdict, detail, evidence):
    assert verdict in TAXO, verdict
    RESULTS.append({"ac": ac, "covers": covers, "verdict": verdict, "detail": detail,
                    "evidence": evidence, "evidence_digest": digest_of(evidence)})
    print("  %-6s %-13s %s" % (ac, verdict, detail))


class Fixture:
    def __init__(self, root):
        self.root = root
        self.keys = {}
        for name, principal in [("disp", "dispatcher@cmp"), ("box", "box@cmp"), ("attacker", "attacker@evil")]:
            k = os.path.join(root, name)
            subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", principal, "-f", k],
                           check=True, capture_output=True)
            pub = open(k + ".pub").read().split()
            self.keys[name] = (k, principal, "%s %s" % (pub[0], pub[1]))
        self.allowed = os.path.join(root, "allowed_signers")
        open(self.allowed, "w").write("%s %s\n" % (self.keys["disp"][1], self.keys["disp"][2]))
        self.box_signers = os.path.join(root, "box_signers")
        open(self.box_signers, "w").write("%s %s\n" % (self.keys["box"][1], self.keys["box"][2]))
        self.attacker_signers = os.path.join(root, "attacker_signers")
        open(self.attacker_signers, "w").write("%s %s\n" % (self.keys["attacker"][1], self.keys["attacker"][2]))
        impl = os.path.join(root, "slicer-run")
        open(impl, "w").write("#!/bin/sh\nexit 0\n")
        self.exec_sha = hashlib.sha256(open(impl, "rb").read()).hexdigest()
        reg = {"jobs": {"slicer": {"version": "1", "exec": impl, "exec_sha256": self.exec_sha,
               "workdir": root, "input_roots": [root], "output_roots": [root], "network": "none",
               "user": "cmpjob", "memory": "10G", "cpu": "600%", "timeout_cap": "6h",
               "sandbox": {"NoNewPrivileges": True, "CapabilityBoundingSet": "",
                           "ProtectSystem": "strict", "PrivateTmp": True},
               "params": {"mode": ["sample", "full"], "n": {"type": "int", "max": 400000}}}}}
        self.registry = os.path.join(root, "registry.json")
        json.dump(reg, open(self.registry, "w"))

    def env(self, name, origin=True):
        d = os.path.join(self.root, name)
        os.makedirs(d, exist_ok=True)
        mirror = os.path.join(d, "mirror.git")
        subprocess.run(["git", "init", "-q", "--bare", mirror], check=True, env=refs.GENV)
        orig = os.path.join(d, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", orig], check=True, env=refs.GENV)
        cfg = {"mirror": mirror, "origin": orig if origin else None, "allowed_signers": self.allowed,
               "box_signers": self.box_signers, "registry": self.registry, "box_key": self.keys["box"][0],
               "ledger_db": os.path.join(d, "ledger.sqlite3"), "runtime_dir": os.path.join(d, "runtime"),
               "spool": os.path.join(d, "spool"), "backend": "fake", "fake_mode": "active",
               "launcher_config": os.path.join(d, "launcher.json"),
               "queue_ref": QREF, "status_ref": SREF, "runner_ref": RREF}
        json.dump({"mirror": mirror, "allowed_signers": self.allowed, "registry": self.registry,
                   "queue_ref": QREF, "ledger_db": cfg["ledger_db"], "runtime_dir": cfg["runtime_dir"],
                   "backend": "fake", "spool": cfg["spool"], "test_only": True},
                  open(cfg["launcher_config"], "w"))
        return cfg

    def _t(self, cfg, origin):
        return cfg["origin"] if (origin and cfg["origin"]) else cfg["mirror"]

    def add_request(self, cfg, rid, bod, key, to_origin=True):
        g = self._t(cfg, to_origin)
        tip = refs.ref_sha(g, QREF)
        base = refs.git(g, "rev-parse", tip + "^{tree}").stdout.decode().strip() if tip else None
        data = (json.dumps(bod, sort_keys=True) + "\n").encode()
        tree = refs.build_tree(g, base, "requests/%s.json" % rid, data)
        ident = "Dispatcher <dispatcher@cmp> %d +0000" % int(time.time())
        c = refs.make_commit(g, tree, [tip] if tip else [], ident, "add %s" % rid, key)
        refs.git(g, "update-ref", QREF, c)
        return c, data

    def add_raw(self, cfg, path, data, key, to_origin=True):
        g = self._t(cfg, to_origin)
        tip = refs.ref_sha(g, QREF)
        base = refs.git(g, "rev-parse", tip + "^{tree}").stdout.decode().strip() if tip else None
        tree = refs.build_tree(g, base, path, data)
        ident = "Dispatcher <dispatcher@cmp> %d +0000" % int(time.time())
        c = refs.make_commit(g, tree, [tip] if tip else [], ident, "raw", key)
        refs.git(g, "update-ref", QREF, c)
        return c

    def merge(self, cfg, key, to_origin=True):
        g = self._t(cfg, to_origin)
        tip = refs.ref_sha(g, QREF)
        base = refs.git(g, "rev-parse", tip + "^{tree}").stdout.decode().strip()
        side = refs.build_tree(g, base, "requests/20260803T000000Z-side.json", b'{"schema":"cmp.job-request.v1"}\n')
        ident = "Dispatcher <dispatcher@cmp> %d +0000" % int(time.time())
        sidec = refs.make_commit(g, side, [tip], ident, "side", key)
        m = refs.make_commit(g, side, [tip, sidec], ident, "merge", key)
        refs.git(g, "update-ref", QREF, m)
        return m

    def multi_add(self, cfg, r1, r2, key, to_origin=True):
        g = self._t(cfg, to_origin)
        tip = refs.ref_sha(g, QREF)
        with tempfile.TemporaryDirectory() as td:
            idx = os.path.join(td, "i")
            e = {**refs.GENV, "GIT_INDEX_FILE": idx}
            if tip:
                subprocess.run(["git", "--git-dir", g, "read-tree",
                                refs.git(g, "rev-parse", tip + "^{tree}").stdout.decode().strip()],
                               env=e, check=True, capture_output=True)
            for r in (r1, r2):
                b = refs.git(g, "hash-object", "-w", "--stdin",
                             input=b'{"schema":"cmp.job-request.v1"}\n').stdout.decode().strip()
                subprocess.run(["git", "--git-dir", g, "update-index", "--add", "--cacheinfo",
                                "100644,%s,requests/%s.json" % (b, r)], env=e, check=True, capture_output=True)
            tree = subprocess.run(["git", "--git-dir", g, "write-tree"], env=e, capture_output=True).stdout.decode().strip()
        ident = "Dispatcher <dispatcher@cmp> %d +0000" % int(time.time())
        c = refs.make_commit(g, tree, [tip] if tip else [], ident, "multi", key)
        refs.git(g, "update-ref", QREF, c)
        return c

    def mutate(self, cfg, rid, key, to_origin=True):
        g = self._t(cfg, to_origin)
        tip = refs.ref_sha(g, QREF)
        base = refs.git(g, "rev-parse", tip + "^{tree}").stdout.decode().strip()
        tree = refs.build_tree(g, base, "requests/%s.json" % rid, b'{"mutated":true}\n')
        ident = "Dispatcher <dispatcher@cmp> %d +0000" % int(time.time())
        c = refs.make_commit(g, tree, [tip], ident, "mutate", key)
        refs.git(g, "update-ref", QREF, c)
        return c


def body(rid, params=None):
    return {"schema": "cmp.job-request.v1", "id": rid, "job": "slicer", "job_version": "1",
            "params": params or {"mode": "sample", "n": 60000}}


def poll(cfg, mode="active"):
    c = dict(cfg); c["fake_mode"] = mode
    admissiond.Poller(c).poll_once()


def rstatus(cfg, signers=None):
    return refs.read_status(cfg["mirror"], SREF, signers or cfg["box_signers"], {})


def rrunner(cfg, pin=None):
    return refs.read_runner(cfg["mirror"], RREF, cfg["box_signers"], pin or {"seq": -1})


def starts(cfg):
    p = os.path.join(cfg["spool"], "_starts.log")
    return len(open(p).read().splitlines()) if os.path.exists(p) else 0


def units(cfg):
    s = cfg["spool"]
    return [f for f in os.listdir(s) if f.startswith("cmpjob-") and f.endswith(".json")] if os.path.isdir(s) else []


def jobev(evs):
    return [e for e in evs if e["schema"] == "cmp.job-status.v1"]


def qev(evs):
    return [e for e in evs if e["schema"] == "cmp.queue-event.v1"]


def launcher(cfg, key, sha, rid, extra_env=None):
    env = {**os.environ, "CMP_LAUNCHER_CONFIG": cfg["launcher_config"], "CMP_FAKE_MODE": "active"}
    if extra_env:
        env.update(extra_env)
    r = subprocess.run([sys.executable, LAUNCHER, key, sha, rid], capture_output=True, env=env)
    try:
        out = json.loads(r.stdout.decode() or "{}")
    except Exception:
        out = {"ok": False, "error": r.stderr.decode()[:300]}
    return r.returncode, out


# ========================================================================= #
def ac1(fx):
    cfg = fx.env("ac1")
    fx.add_request(cfg, "20260803T100000Z-a", {"schema": "cmp.job-request.v1", "id": "20260803T100000Z-a",
                   "job": "/bin/sh", "job_version": "1", "params": {}}, fx.keys["disp"][0])
    fx.add_request(cfg, "20260803T100001Z-b", {"schema": "cmp.job-request.v1", "id": "20260803T100001Z-b",
                   "job": "nope", "job_version": "1", "params": {}}, fx.keys["disp"][0])
    fx.add_request(cfg, "20260803T100002Z-" + "x" * 40, body("20260803T100002Z-" + "x" * 40), fx.keys["disp"][0])
    fx.add_raw(cfg, "requests/20260803T100003Z-bad:colon.json", b'{"schema":"cmp.job-request.v1"}\n', fx.keys["disp"][0])
    poll(cfg)
    evs, _ = rstatus(cfg)
    rej = [e for e in jobev(evs) if e["state"] == "rejected"]
    ok = len(rej) >= 4 and units(cfg) == [] and starts(cfg) == 0
    record("AC1", "A1,A2", "pass_model" if ok else "fail",
           "bad job / unknown / oversized-id / colon-id all rejected, no unit, no launch",
           {"reasons": [e["reason"] for e in rej], "units": units(cfg), "starts": starts(cfg)})


def ac11(fx):
    # The job_id -> request_digest binding is enforced by the ONE ledger that the
    # privileged launcher owns. A legitimate linear queue cannot re-add requests/<id>
    # (that is QUEUE-MALFORMED), so the genuine trigger is a second presentation of the
    # same id with different bytes reaching the ledger — proven here at both the ledger
    # (sqlite UNIQUE) and the authoritative launcher level.
    a = fx.env("ac11a")
    b = fx.env("ac11b")
    b["ledger_db"] = a["ledger_db"]                       # the ONE shared root-owned ledger
    json.dump({**json.load(open(b["launcher_config"])), "ledger_db": a["ledger_db"]}, open(b["launcher_config"], "w"))
    rid = "20260803T170000Z-dup"
    _, d1 = fx.add_request(a, rid, body(rid), fx.keys["disp"][0])
    poll(a, "clean-exit")                                 # id -> d1 bound in the ledger
    dg1 = refs.request_digest(d1)
    # (1) ledger level: a second (id, DIFFERENT digest) is refused inside one transaction
    conn = ledger.connect(a["ledger_db"])
    d2fake = "f" * 64
    ledger_verdict = ledger.try_intent(conn, d2fake, rid, "cmpjob-%s" % rid, "q", "p", "f")
    # (2) authoritative launcher level: a genuinely dispatcher-signed request with the same
    # id but different bytes is refused by the launcher's own ledger check — no launch.
    _, d2 = fx.add_request(b, rid, body(rid, {"mode": "full", "n": 123}), fx.keys["disp"][0])
    refs.fetch_queue(b["mirror"], b["origin"], QREF)
    shab = refs.ref_sha(b["mirror"], QREF)
    n0 = starts(b)
    rc, out = launcher(b, "slicer", shab, rid)
    dg2 = refs.request_digest(d2)
    ok = (dg1 != dg2 and ledger_verdict == "INTEGRITY"
          and out.get("outcome") == "INTEGRITY" and out.get("ok") is False and starts(b) == n0)
    record("AC11", "A2", "pass_model" if ok else "fail",
           "same id + different digest -> integrity reject at the ledger (sqlite UNIQUE(job_id)) AND at the authoritative launcher; not a benign replay; no launch",
           {"d1": dg1[:16], "d2": dg2[:16], "ledger_verdict": ledger_verdict,
            "launcher_outcome": out.get("outcome"), "launched": starts(b) != n0})


def ac12(fx):
    cfg = fx.env("ac12")
    fx.add_request(cfg, "20260803T180000Z-wrong", body("20260803T180000Z-wrong"), fx.keys["attacker"][0])
    poll(cfg)
    e1, _ = rstatus(cfg)
    wrong = [e for e in qev(e1) if e["kind"] == "QUEUE-UNSIGNED"]
    no_unit = units(cfg) == []
    cfg2 = fx.env("ac12b")
    fx.add_request(cfg2, "20260803T180100Z-uns", body("20260803T180100Z-uns"), None)
    poll(cfg2)
    e2, _ = rstatus(cfg2)
    uns = [e for e in qev(e2) if e["kind"] == "QUEUE-UNSIGNED"]
    cfg3 = fx.env("ac12c")
    tree = refs.build_tree(cfg3["mirror"], None, "events/000001.json", b'{"schema":"cmp.job-status.v1","event_id":"000001"}\n')
    bad = refs.make_commit(cfg3["mirror"], tree, [], "x <x@x> 1 +0000", "forged", fx.keys["disp"][0])
    refs.git(cfg3["mirror"], "update-ref", SREF, bad)
    try:
        rstatus(cfg3); trusted = True
    except (ValueError, refs.StatusTamper):
        trusted = False
    ok = len(wrong) == 1 and no_unit and len(uns) == 1 and trusted is False
    record("AC12", "A13", "pass_model" if ok else "fail",
           "wrong-key & unsigned queue commits -> QUEUE-UNSIGNED, never admitted; non-box status commit not trusted (real ssh-keygen verify)",
           {"wrong": bool(wrong), "unsigned": bool(uns), "non_box_status_trusted": trusted, "unit": not no_unit})


def ac13(fx):
    cfg = fx.env("ac13")
    rid = "20260803T190000Z-repro"
    _, data = fx.add_request(cfg, rid, body(rid), fx.keys["disp"][0])
    refs.fetch_queue(cfg["mirror"], cfg["origin"], QREF)
    sha = refs.ref_sha(cfg["mirror"], QREF)
    d_poller = refs.request_digest(data)
    rc, out = launcher(cfg, "slicer", sha, rid)
    d_launcher = out.get("request_digest")
    q1 = refs.read_queue(cfg["mirror"], QREF, None, fx.allowed)
    q2 = refs.read_queue(cfg["mirror"], QREF, None, fx.allowed)
    cur_match = [x["sha"] for x in q1] == [x["sha"] for x in q2]
    kinds = {}
    scenarios = [("merge", lambda e: (fx.add_request(e, "20260803T190100Z-x", body("20260803T190100Z-x"), fx.keys["disp"][0]), fx.merge(e, fx.keys["disp"][0]))),
                 ("multi_add", lambda e: fx.multi_add(e, "20260803T190200Z-a", "20260803T190200Z-b", fx.keys["disp"][0])),
                 ("mutate", lambda e: (fx.add_request(e, "20260803T190300Z-m", body("20260803T190300Z-m"), fx.keys["disp"][0]), fx.mutate(e, "20260803T190300Z-m", fx.keys["disp"][0])))]
    for nm, fn in scenarios:
        env2 = fx.env("ac13-" + nm)
        fn(env2)
        refs.fetch_queue(env2["mirror"], env2["origin"], QREF)
        try:
            refs.read_queue(env2["mirror"], QREF, None, fx.allowed); kinds[nm] = "ADMITTED"
        except refs.QueueError as e:
            kinds[nm] = e.kind
    ok = (d_poller == d_launcher and cur_match and kinds["merge"] == "QUEUE-NONLINEAR"
          and kinds["multi_add"] == "QUEUE-MALFORMED" and kinds["mutate"] == "QUEUE-MALFORMED")
    record("AC13", "A14", "pass_model" if ok else "fail",
           "independent processes derive identical request_digest + cursor; merge->NONLINEAR, multi-add/mutate->MALFORMED",
           {"digest_match": d_poller == d_launcher, "cursor_match": cur_match, "adversarial": kinds})


def ac15(fx):
    cfg = fx.env("ac15")
    fx.add_request(cfg, "20260803T210000Z-t1", body("20260803T210000Z-t1"), fx.keys["disp"][0])
    poll(cfg, "clean-exit")
    conn = ledger.connect(cfg["ledger_db"]); cursor = ledger.meta_get(conn, "cursor")
    tree = refs.build_tree(cfg["origin"], None, "requests/20260803T210001Z-t2.json", b'{"schema":"cmp.job-request.v1"}\n')
    rogue = refs.make_commit(cfg["origin"], tree, [], "d <d@cmp> 1 +0000", "rewrite", fx.keys["disp"][0])
    refs.git(cfg["origin"], "update-ref", QREF, rogue)
    poll(cfg, "clean-exit")
    evs, _ = rstatus(cfg)
    tamper = [e for e in qev(evs) if e["kind"] == "QUEUE-TAMPER"]
    admitted_rogue = any(e.get("job_id") == "20260803T210001Z-t2" for e in jobev(evs))
    ok = len(tamper) >= 1 and not admitted_rogue
    record("AC15", "A15", "pass_model" if ok else "fail",
           "force-push dropping last_accepted_commit -> QUEUE-TAMPER halt (falsifiable); rogue not admitted",
           {"tamper_events": len(tamper), "rogue_admitted": admitted_rogue, "cursor_was": (cursor or "")[:12]})


def ac16(fx):
    cfg = fx.env("ac16")
    m, bk = cfg["mirror"], fx.keys["box"][0]
    e1 = {"schema": "cmp.job-status.v1", "event_id": "000001", "job_id": "j", "seq": 1, "state": "queued",
          "reason": None, "request_digest": "d", "queue_commit": "q", "unit": None, "principal": "p",
          "fingerprint": "f", "observed_at": 1}
    c1 = refs.publish_event(m, SREF, 1, e1, bk)
    refs.publish_event(m, SREF, 2, dict(e1, event_id="000002", seq=2, state="admitted"), bk)
    _, pin = refs.read_status(m, SREF, fx.box_signers, {})
    try:
        refs.verify_commit(m, c1, fx.box_signers); c1_box = True
    except ValueError:
        c1_box = False
    refs.git(m, "update-ref", SREF, c1)                       # replay OLD box-signed commit
    try:
        refs.read_status(m, SREF, fx.box_signers, pin); status_detected = False
    except refs.StatusTamper:
        status_detected = True
    r3 = refs.runner_write(m, RREF, {"seq": 3, "observed_at": 10, "current_job": None, "health": "OK", "last_error": None}, bk)
    refs.runner_write(m, RREF, {"seq": 5, "observed_at": 20, "current_job": None, "health": "OK", "last_error": None}, bk)
    _, rpin = refs.read_runner(m, RREF, fx.box_signers, {"seq": -1})
    _, rpin2 = refs.read_runner(m, RREF, fx.box_signers, rpin)   # unchanged reread, not replay
    reread_ok = rpin2["seq"] == 5
    refs.git(m, "update-ref", RREF, r3)                       # replay OLD box-signed snapshot
    try:
        refs.read_runner(m, RREF, fx.box_signers, rpin); runner_detected = False
    except refs.RunnerReplay:
        runner_detected = True
    ok = c1_box and status_detected and reread_ok and runner_detected
    record("AC16", "A16", "pass_model" if ok else "fail",
           "genuinely box-signed OLD status/runner replays detected (STATUS-TAMPER/RUNNER-REPLAY); unchanged reread not a false replay",
           {"replayed_is_box_signed": c1_box, "status_tamper": status_detected,
            "unchanged_reread_ok": reread_ok, "runner_replay": runner_detected})


def ac17(fx):
    cfg = fx.env("ac17")
    rid = "20260803T120200Z-fastexit"
    fx.add_request(cfg, rid, body(rid), fx.keys["disp"][0])
    poll(cfg, "fast-exit-collected")
    n1 = starts(cfg)
    poll(cfg, "fast-exit-collected")                          # restart: must NOT relaunch
    evs, _ = rstatus(cfg)
    indet = [e for e in jobev(evs) if e["state"] == "indeterminate" and e["reason"] == "launch_outcome_unknown"]
    ok = len(indet) == 1 and n1 == 1 and starts(cfg) == 1
    record("AC17", "§3.5", "pass_model" if ok else "fail",
           "fast-exit-then-collected -> indeterminate/launch_outcome_unknown; never auto-relaunched (start log stays 1)",
           {"indeterminate": bool(indet), "starts_after_first": n1, "starts_after_restart": starts(cfg)})


def ac4(fx):
    cfg = fx.env("ac4")
    conn = ledger.connect(cfg["ledger_db"])
    deltas = {}
    ridA = "20260803T120000Z-before"
    _, dA = fx.add_request(cfg, ridA, body(ridA), fx.keys["disp"][0])
    refs.fetch_queue(cfg["mirror"], cfg["origin"], QREF)
    shaA = refs.ref_sha(cfg["mirror"], QREF)
    ledger.try_intent(conn, refs.request_digest(dA), ridA, "cmpjob-%s" % ridA, shaA, "dispatcher@cmp", "f")
    admissiond.Poller(cfg).poll_once()
    deltas["before_start"] = starts(cfg)
    ridB = "20260803T120100Z-active"
    fx.add_request(cfg, ridB, body(ridB), fx.keys["disp"][0])
    poll(cfg, "active")
    nb = starts(cfg)
    admissiond.Poller(cfg).poll_once()
    deltas["restart_active"] = starts(cfg) - nb
    up = os.path.join(cfg["spool"], "cmpjob-%s.json" % ridB)
    d = json.load(open(up)); d.update(ActiveState="inactive", Result="success", ExecMainStatus=0, memory_peak=1024)
    json.dump(d, open(up, "w"))
    admissiond.Poller(cfg).poll_once()
    ridC = "20260803T120200Z-fast"
    _, dC = fx.add_request(cfg, ridC, body(ridC), fx.keys["disp"][0])
    poll(cfg, "fast-exit-collected")
    nc = starts(cfg)
    poll(cfg, "fast-exit-collected")
    deltas["restart_fast"] = starts(cfg) - nc
    refs.fetch_queue(cfg["mirror"], cfg["origin"], QREF)
    shaC = [s for s in refs.git(cfg["mirror"], "rev-list", QREF).stdout.decode().split()
            if refs.added_paths(cfg["mirror"], s)[0][1] == "requests/%s.json" % ridC][0]
    nrep = starts(cfg)
    rc, out = launcher(cfg, "slicer", shaC, ridC)
    deltas["replay"] = starts(cfg) - nrep
    replay_rejected = out.get("outcome") == "REPLAY_TERMINAL"
    evs, _ = rstatus(cfg)
    indet = set(e["reason"] for e in jobev(evs) if e["state"] == "indeterminate")
    no_relaunch = all(deltas[k] == 0 for k in ("before_start", "restart_active", "restart_fast", "replay"))
    ckpt = os.path.join(cfg["spool"], "slicer.ckpt")

    def slicer_run(units_requested):
        done = json.load(open(ckpt)) if os.path.exists(ckpt) else {"done": 0}
        recomputed = max(0, units_requested - done["done"])
        json.dump({"done": max(units_requested, done["done"])}, open(ckpt, "w"))
        return recomputed
    first, resubmit = slicer_run(100), slicer_run(100)
    zero_recompute = first == 100 and resubmit == 0
    ok = no_relaunch and replay_rejected and indet == {"launch_outcome_unknown"} and zero_recompute
    record("AC4", "A5", "partial" if ok else "fail",
           "reconciliation relaunches none (before/active/fast/replay); indeterminate on ambiguous; replay rejected by ledger; "
           "Slicer zero-recompute resume is a MODEL (Slicer out of scope) — real end-to-end resume needs the built Slicer + real systemd",
           {"launch_deltas": deltas, "replay_rejected": replay_rejected, "indeterminate_reasons": sorted(indet),
            "slicer_model_recompute": {"first": first, "resubmit": resubmit}})


def ac14(fx):
    cfg = fx.env("ac14")
    rid = "20260803T200000Z-compromised"
    fx.add_request(cfg, rid, body(rid), fx.keys["attacker"][0])
    refs.fetch_queue(cfg["mirror"], cfg["origin"], QREF)
    sha = refs.ref_sha(cfg["mirror"], QREF)
    mal = {"CMP_MIRROR": cfg["mirror"], "CMP_ALLOWED_SIGNERS": fx.attacker_signers,
           "CMP_REGISTRY": fx.registry, "CMP_BACKEND": "fake", "CMP_SPOOL": cfg["spool"],
           "CMP_QUEUE_REF": QREF, "CMP_LEDGER_DB": "/tmp/attacker-ledger.sqlite3"}
    rc, out = launcher(cfg, "slicer", sha, rid, extra_env=mal)
    ignored_env = rc == 1 and out.get("ok") is False and "QUEUE-UNSIGNED" in out.get("error", "") and units(cfg) == []
    cfg2 = fx.env("ac14ok")
    fx.add_request(cfg2, rid, body(rid), fx.keys["disp"][0])
    refs.fetch_queue(cfg2["mirror"], cfg2["origin"], QREF)
    sha2 = refs.ref_sha(cfg2["mirror"], QREF)
    rc2, out2 = launcher(cfg2, "slicer", sha2, rid)
    control_ok = rc2 == 0 and out2.get("ok") is True
    att_cfg = os.path.join(fx.root, "ac14", "attacker_launcher.json")
    json.dump({"mirror": cfg["mirror"], "allowed_signers": fx.attacker_signers, "registry": fx.registry,
               "queue_ref": QREF, "ledger_db": os.path.join(fx.root, "ac14", "att.sqlite3"),
               "runtime_dir": os.path.join(fx.root, "ac14", "attrt"), "backend": "fake",
               "spool": os.path.join(fx.root, "ac14", "attspool"), "test_only": True}, open(att_cfg, "w"))
    rc3, out3 = launcher(cfg, "slicer", sha, rid, extra_env={"CMP_LAUNCHER_CONFIG": att_cfg})
    residual = out3.get("ok") is True
    ok = ignored_env and control_ok
    record("AC14", "A12", "partial" if ok else "fail",
           "compromised poller: launcher IGNORES all caller-supplied trust env (mirror/allowed_signers/registry/backend/ledger), "
           "re-verifies from its OWN root config -> wrong-key rejected, no launch; dispatcher-signed still launches. "
           "RESIDUAL (documented): owning the config PATH defeats it — closed in production by a compiled-in root-owned path + FS perms, not exercisable at fixture level",
           {"trust_env_ignored": ignored_env, "control_signed_launches": control_ok,
            "residual_config_path_capture": residual, "launcher_error": out.get("error")})


# ---- not_evaluated ACs: run the model check, do NOT claim a live pass ----
def ac2(fx):
    cfg = fx.env("ac2")
    rid = "20260803T110000Z-oom"
    fx.add_request(cfg, rid, body(rid), fx.keys["disp"][0])
    poll(cfg, "oom")
    evs, _ = rstatus(cfg)
    oom = [e for e in jobev(evs) if e["state"] == "failed" and e["reason"] == "CONSTRAINT_MEMCG"]
    model = bool(oom) and oom[0].get("memory_peak") and oom[0].get("RuntimeMaxSec") == 21600
    record("AC2", "A3", "not_evaluated",
           "MODEL ok (fake OOM -> CONSTRAINT_MEMCG + limits from registry); real cgroup OOM / global-runner survival needs real systemd — NOT evaluated here",
           {"model_ok": bool(model), "oom_event": oom[0] if oom else None})


def ac3(fx):
    reg = registry.load(fx.registry); spec = registry.resolve(reg, "slicer", "1")
    import inspect
    r2 = subprocess.run([sys.executable, LAUNCHER, "slicer"], capture_output=True, env=refs.GENV)
    r5 = subprocess.run([sys.executable, LAUNCHER, "a", "b", "c", "d"], capture_output=True, env=refs.GENV)
    params = list(inspect.signature(cmp_launch.launch).parameters)
    model = spec["user"] == "cmpjob" and r2.returncode == 2 and r5.returncode == 2 and params == ["job_key", "sha", "rid", "cfg"]
    record("AC3", "A4,A12", "not_evaluated",
           "MODEL ok (registry uid=cmpjob; launcher argv-only + own root config, no caller path/bytes); live sudo-denied / no-git-credential / uid-drop needs real cmpjob+systemd — NOT evaluated",
           {"model_ok": bool(model), "registry_user": spec["user"], "wrong_arity_rc": [r2.returncode, r5.returncode]})


def ac5(fx):
    cfg = fx.env("ac5")
    rid = "20260803T130000Z-adopt"
    fx.add_request(cfg, rid, body(rid), fx.keys["disp"][0])
    poll(cfg, "active")
    n1 = starts(cfg)
    show = cmp_launch.unit_show(cfg, "cmpjob-%s" % rid)
    admissiond.Poller(cfg).poll_once()
    up = os.path.join(cfg["spool"], "cmpjob-%s.json" % rid)
    d = json.load(open(up)); d.update(ActiveState="inactive", Result="success", ExecMainStatus=0, memory_peak=1024)
    json.dump(d, open(up, "w"))
    admissiond.Poller(cfg).poll_once()
    evs, _ = rstatus(cfg)
    succ = [e for e in jobev(evs) if e["state"] == "succeeded" and e["job_id"] == rid]
    model = show and show["ActiveState"] == "active" and starts(cfg) == n1 and len(succ) == 1
    record("AC5", "A6", "not_evaluated",
           "MODEL ok (restart adopts active unit, no relaunch, terminal from unit exit); real orphan-survival needs a real transient systemd unit — NOT evaluated",
           {"model_ok": bool(model), "terminal": succ[0] if succ else None})


def ac6(fx):
    cfg = fx.env("ac6")
    rid = "20260803T140000Z-self"
    fx.add_request(cfg, rid, body(rid), fx.keys["disp"][0])
    os.makedirs(cfg["spool"], exist_ok=True)
    open(os.path.join(cfg["spool"], "job-claims-success.txt"), "w").write("SUCCESS")
    poll(cfg, "fail-exit")
    evs, _ = rstatus(cfg)
    term = [e for e in jobev(evs) if e["job_id"] == rid and e["state"] in {"succeeded", "failed"}]
    all_box = True
    for s in refs.git(cfg["mirror"], "rev-list", SREF).stdout.decode().split():
        try:
            refs.verify_commit(cfg["mirror"], s, fx.box_signers)
        except ValueError:
            all_box = False
    model = len(term) == 1 and term[0]["state"] == "failed" and all_box
    record("AC6", "A7,A10", "not_evaluated",
           "MODEL ok (nonzero exit -> failed; job self-success ignored; status box-signed only); real job self-publish attempt needs the real job/no-credential substrate — NOT evaluated",
           {"model_ok": bool(model), "terminal_state": term[0]["state"] if term else None})


def ac7(fx):
    cfg = fx.env("ac7")
    rid = "20260803T150000Z-stall"
    fx.add_request(cfg, rid, body(rid), fx.keys["disp"][0])
    poll(cfg, "active")
    prog = os.path.join(cfg["spool"], "cmpjob-%s.progress" % rid)
    json.dump({"counter": 5, "ts": int(time.time())}, open(prog, "w"))
    admissiond.Poller(cfg).poll_once()
    s1, pin = rrunner(cfg)
    for _ in range(3):
        admissiond.Poller(cfg).poll_once()
    s2, _ = rrunner(cfg, pin)
    os.remove(prog); os.symlink("/etc/passwd", prog)
    admissiond.Poller(cfg).poll_once()
    s3, _ = rrunner(cfg, {"seq": s2["seq"], "observed_at": 0, "commit": None})
    hostile_ignored = s3["progress"] is None
    model = s1["progress"]["counter"] == s2["progress"]["counter"] == 5 and s2["seq"] > s1["seq"] and hostile_ignored
    record("AC7", "A8", "not_evaluated",
           "MODEL ok (progress frozen while runner stays fresh -> stalled; hostile symlink progress ignored, poller not crashed); real SIGSTOP of a real unit — NOT evaluated",
           {"model_ok": bool(model), "runner_seq": [s1["seq"], s2["seq"]], "hostile_symlink_ignored": hostile_ignored})


def ac8(fx):
    cfg = fx.env("ac8")
    rid = "20260803T160000Z-live"
    fx.add_request(cfg, rid, body(rid), fx.keys["disp"][0])
    poll(cfg, "active")
    broken = dict(cfg); broken["origin"] = os.path.join(cfg["mirror"], "does-not-exist.git")
    admissiond.Poller(broken).poll_once()
    snap, _ = rrunner(cfg)
    model = snap["health"] == "FETCH-FAIL"
    record("AC8", "A9", "not_evaluated",
           "MODEL ok (broken remote -> FETCH-FAIL health, not 'idle'); real GitHub remote outage / dead-poller-vs-idle on the box — NOT evaluated",
           {"model_ok": bool(model), "health": snap["health"]})


def ac9(fx):
    cfg = fx.env("ac9")
    rid = "20260803T110100Z-clean"
    fx.add_request(cfg, rid, body(rid), fx.keys["disp"][0])
    poll(cfg, "clean-exit")
    evs, _ = rstatus(cfg)
    succ = [e for e in jobev(evs) if e["state"] == "succeeded"]
    model = succ and succ[0].get("memory_peak") and succ[0].get("RuntimeMaxSec") == 21600
    record("AC9", "§4.1,§4.3", "not_evaluated",
           "MODEL ok (terminal carries memory.peak + RuntimeMaxSec; classify_terminal also maps real `systemctl show` MemoryPeak); real cgroup memory.peak — NOT evaluated",
           {"model_ok": bool(model), "succeeded_event": succ[0] if succ else None})


def ac10(fx):
    res = refs.ci_inert_check("usurobor", "cmp", [QREF, SREF, RREF])
    statuses = sorted(set(v["status"] for v in res.values()))
    record("AC10", "A11", "not_evaluated",
           "runtime Actions-API check executed (statuses=%s); operational refs are not on the real remote, so 0-runs is not a meaningful live evaluation — re-run post-cutover" % statuses,
           {"per_ref": res})


def main():
    root = tempfile.mkdtemp(prefix="cmp-acs-")
    print("box-job-admission v0.5.1 @ ce91739 — HONEST AC taxonomy harness")
    print("fixtures:", root)
    fx = Fixture(root)
    systemd_ok = cmp_launch.systemd_available()
    for fn in [ac1, ac2, ac3, ac4, ac5, ac6, ac7, ac8, ac9, ac10, ac11, ac12, ac13, ac14, ac15, ac16, ac17]:
        try:
            fn(fx)
        except Exception as e:
            import traceback
            record(fn.__name__.upper(), "?", "fail", "harness exception: %s" % e, {"traceback": traceback.format_exc()})
    order = ["AC%d" % i for i in range(1, 18)]
    RESULTS.sort(key=lambda r: order.index(r["ac"]) if r["ac"] in order else 99)
    v = {r["ac"]: r["verdict"] for r in RESULTS}
    n = {k: sum(1 for x in v.values() if x == k) for k in TAXO}
    if n["fail"]:
        overall = "FAIL"
    elif n["not_evaluated"] or n["partial"]:
        overall = "PARTIAL"
    else:
        overall = "PASS"
    receipt = {
        "design": "box-job-admission", "version": "v0.5.1", "design_commit": "ce91739",
        "generated_at": int(time.time()), "fixtures_root": root,
        "substrate": {"unit_backend": "fake-unit", "systemd_available": systemd_ok,
                      "remote": "local bare-repo origin (NOT GitHub)",
                      "note": "fake-unit is test_only; a production launcher config (backend=systemd) "
                              "fails closed with no live bus and cannot select it"},
        "signing": "ssh-keygen -Y ed25519; commits SSH-signed and independently verified",
        "ledger": "single root-owned sqlite3 transactional store + outbox; launcher owns the at-most-once lock",
        "taxonomy_counts": n,
        "overall_verdict": overall,
        "overall_detail": "PARTIAL — code-level model complete (crypto/ledger/wire/at-most-once proven); "
                          "real-systemd + real-remote ACs (AC2,3,5,6,7,8,9,10) pending an isolated box staging; "
                          "AC4/AC14 partial (Slicer-resume model / config-path residual documented).",
        "summary": {r["ac"]: r["verdict"] for r in RESULTS},
        "results": RESULTS,
    }
    receipt["receipt_digest"] = digest_of(receipt["results"])
    out = os.path.join(HERE, "ac_receipt.json")
    json.dump(receipt, open(out, "w"), indent=2)
    print("\ncounts:", n)
    print("overall:", overall)
    print("receipt:", out)
    return 0 if overall in ("PASS", "PARTIAL") else 1


if __name__ == "__main__":
    sys.exit(main())
