#!/usr/bin/env python3
"""cmp-poller (v0) — unprivileged box job runner. stdlib-only.

Pulls refs/heads/jobs/queue, validates each request {id, mode, n} against the
single-entry box-local registry, launches the one fixed `slicer` unit via
systemd-run (non-root cmpjob, cgroup+time bounded, network-off), force-updates a
refs/heads/jobs/status snapshot (runner.json heartbeat + status/<id>.json), and
enforces at-most-once via an O_EXCL launched/<id> marker + single-active check,
adopting an already-running cmpjob-<id> unit on restart (never kills/duplicates).

Git supplies only {id, mode, n}. The registry names a job; git never supplies a
command, exec path, argv, or resource policy. Pure core (validate/observe) is
separated from the git/systemd effect shell so AC2 is testable with no systemd.
"""
import os, sys, json, time, re, subprocess

ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


# ---- pure core -----------------------------------------------------------
def load_registry(path):
    """Parse the flat single-entry registry (a YAML mapping subset). stdlib-only."""
    reg = {}
    for line in open(path):
        line = line.split("#", 1)[0].strip()
        if ":" in line:
            k, v = line.split(":", 1)
            reg[k.strip()] = v.strip()
    return reg


def validate(req, reg):
    """Pure. Return (ok, reason). This is the AC2 admission boundary."""
    if not isinstance(req, dict):
        return False, "not-an-object"
    if set(req) != {"id", "mode", "n"}:
        return False, "schema"                      # rejects extra keys e.g. {"job": "/bin/sh"}
    if not isinstance(req["id"], str) or not ID_RE.match(req["id"]):
        return False, "bad-id"
    if req["mode"] not in reg["modes"].split(","):
        return False, "unknown-mode"
    n = req["n"]
    if not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > int(reg["n_max"]):
        return False, "n-out-of-bounds"
    return True, "ok"


def observe(rid, unit, show, now):
    """Derive terminal state from the unit itself (a job cannot declare success)."""
    if show.get("ActiveState") == "active":
        return {"id": rid, "state": "running", "unit": unit, "observed_at": now}
    res, code = show.get("Result", ""), show.get("ExecMainStatus", "")
    ok = res == "success" and code in ("0", "")
    return {"id": rid, "state": "succeeded" if ok else "failed", "unit": unit,
            "result": res, "exit": code, "observed_at": now}


def dispatch(requests, reg, state_dir, launch, unit_active, unit_show, now):
    """Pure-ish core of admission. Effects are the three injected callables +
    marker/status files under state_dir. Returns {id: status}. AC2 lives here:
    a rejected request writes a status and NEVER calls `launch`."""
    ld = os.path.join(state_dir, "launched"); sd = os.path.join(state_dir, "status")
    os.makedirs(ld, exist_ok=True); os.makedirs(sd, exist_ok=True)
    statuses, active = {}, unit_active()
    for req in requests:
        ok, reason = validate(req, reg)
        if not ok:
            rid = req["id"] if isinstance(req, dict) and ID_RE.match(str(req.get("id"))) else "malformed"
            statuses[rid] = {"id": rid, "state": "rejected", "reason": reason, "observed_at": now}
            continue
        rid = req["id"]; unit = "cmpjob-%s" % rid
        show = unit_show(unit)
        if show.get("LoadState") not in (None, "", "not-found"):
            statuses[rid] = observe(rid, unit, show, now)     # adopt; never relaunch
            continue
        if active:
            statuses[rid] = {"id": rid, "state": "queued", "reason": "job-active", "observed_at": now}
            continue
        try:
            os.close(os.open(os.path.join(ld, rid), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            statuses[rid] = {"id": rid, "state": "launched", "reason": "marker", "observed_at": now}
            continue
        pp = write_params(state_dir, reg, req)
        launch(unit, {"user": reg["user"], "exec": reg["exec"], "memory": reg["memory"],
                      "cpu": reg["cpu"], "timeout": int(reg["timeout"]), "params_path": pp})
        statuses[rid] = {"id": rid, "state": "launched", "unit": unit, "observed_at": now}
        active = True                                          # single active job at a time
    for rid, st in statuses.items():
        write_json(os.path.join(sd, "%s.json" % rid), st)
    return statuses


# ---- effect shell --------------------------------------------------------
def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, sort_keys=True); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)


def write_params(state_dir, reg, req):
    """Validated params file the unit reads. dataset comes from the REGISTRY."""
    pp = os.path.join(state_dir, "params", "%s.json" % req["id"])
    os.makedirs(os.path.dirname(pp), exist_ok=True)
    write_json(pp, {"id": req["id"], "mode": req["mode"], "n": req["n"], "dataset": reg["dataset"],
                    "progress": os.path.join(state_dir, "progress", "%s.json" % req["id"]),
                    "receipt": os.path.join(state_dir, "receipt", "%s.json" % req["id"])})
    return pp


def systemd_launch(unit, spec):
    r = subprocess.run(
        ["systemd-run", "--unit=%s" % unit, "--collect", "--uid=%s" % spec["user"],
         "--property=MemoryMax=%s" % spec["memory"], "--property=MemorySwapMax=0",
         "--property=CPUQuota=%s" % spec["cpu"], "--property=RuntimeMaxSec=%d" % spec["timeout"],
         "--property=IPAddressDeny=any", "--property=NoNewPrivileges=yes",
         "--", spec["exec"], spec["params_path"]], capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("systemd-run failed: %s" % r.stderr.decode()[:200])


def git(g, *a, input=None, env=None):
    return subprocess.run(["git", "-C", g, *a], capture_output=True, input=input, env=env)


def systemctl(*a):
    return subprocess.run(["systemctl", *a], capture_output=True).stdout.decode()


def unit_show(unit):
    out = systemctl("show", unit, "-p", "LoadState,ActiveState,Result,ExecMainStatus", "--no-page")
    return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)


def unit_active():
    return bool(systemctl("list-units", "cmpjob-*", "--state=active", "--no-legend", "--plain").strip())


def read_requests(g, qref):
    tip = git(g, "rev-parse", "--verify", "-q", qref).stdout.decode().strip()
    reqs = []
    for name in (git(g, "ls-tree", "-r", "--name-only", tip).stdout.decode().split() if tip else []):
        if name.startswith("requests/") and name.endswith(".json"):
            try:
                reqs.append(json.loads(git(g, "show", "%s:%s" % (tip, name)).stdout))
            except ValueError:
                reqs.append({"id": name[9:-5], "mode": None, "n": None})   # malformed -> rejected
    return reqs


def publish(g, o, sref, files, now):
    env = dict(os.environ, GIT_INDEX_FILE=os.path.join(g, ".git", "index.status"),
               GIT_AUTHOR_NAME="cmp-poller", GIT_AUTHOR_EMAIL="poller@cn-sigma",
               GIT_COMMITTER_NAME="cmp-poller", GIT_COMMITTER_EMAIL="poller@cn-sigma")
    for path, data in files.items():
        h = git(g, "hash-object", "-w", "--stdin", input=data).stdout.decode().strip()
        git(g, "update-index", "--add", "--cacheinfo", "100644,%s,%s" % (h, path), env=env)
    tree = git(g, "write-tree", env=env).stdout.decode().strip()
    c = git(g, "commit-tree", tree, "-m", "status %d" % now, env=env).stdout.decode().strip()
    git(g, "update-ref", sref, c)
    git(g, "push", "--force", o, "%s:%s" % (sref, sref))


def poll_once(cfg):
    g, o = cfg["gitdir"], cfg["origin"]
    qref = cfg.get("queue_ref", "refs/heads/jobs/queue"); sref = cfg.get("status_ref", "refs/heads/jobs/status")
    now = int(time.time())
    health = "OK" if git(g, "fetch", o, "+%s:%s" % (qref, qref)).returncode == 0 else "FETCH-FAIL"
    st = dispatch(read_requests(g, qref) if health == "OK" else [], load_registry(cfg["registry"]),
                  cfg["state_dir"], systemd_launch, unit_active, unit_show, now)
    files = {"status/%s.json" % r: json.dumps(s, sort_keys=True).encode() for r, s in st.items()}
    files["runner.json"] = json.dumps({"observed_at": now, "health": health,
        "active": [s["unit"] for s in st.values() if s["state"] == "running"] or None}, sort_keys=True).encode()
    publish(g, o, sref, files, now)


if __name__ == "__main__":
    poll_once(json.load(open(sys.argv[1])))
