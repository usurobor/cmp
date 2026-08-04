"""cmp_launch.py — the hardened root launcher (§4.2) + pluggable unit backend (§4.1).

Invoked ONLY as:  cmp-launch <registry-key> <queue-commit-sha> <request-id>

TRUST IS OWNED BY THE PRIVILEGED SIDE (Pi impl-audit #1). The launcher reads its
mirror / allowed_signers / registry / ledger / backend from its OWN root-owned
config file (default /etc/cmp-jobs/launcher.json), NOT from anything the caller
supplies. It ignores every CMP_* trust env var a compromised poller might set; the
three argv tokens are the only caller input. So a poller that is fully compromised
can neither pick the trust store, the registry, the mirror, nor swap in a fake
backend — it can only ask for a job by identity, which the launcher re-verifies.

It then, in ONE sqlite transaction (ledger.try_intent), takes the durable
at-most-once launch lock (Pi #2) — surviving systemd `--collect` — records the
verified principal/fingerprint, delivers the exact verified request bytes to the
job as a root-owned read-only file whose digest is bound in the ledger (Pi #3), and
launches. Backend selection FAILS CLOSED (Pi #4): a systemd backend with no live
bus refuses; the fake backend is reachable only under an explicit test_only config
a production launcher will not carry.
"""
import os, sys, json, subprocess, tempfile, shutil, time, hashlib
import refs
import registry
import ledger

DEFAULT_CONFIG = "/etc/cmp-jobs/launcher.json"


class LaunchError(Exception):
    pass


def runtime_max_sec(cap):
    m = {"h": 3600, "m": 60, "s": 1}
    cap = str(cap)
    return int(cap[:-1]) * m[cap[-1]] if cap and cap[-1] in m else int(cap)


def runtime_bytes(mem):
    u = {"G": 1024**3, "M": 1024**2, "K": 1024}.get(mem[-1].upper())
    return int(mem[:-1]) * u if u else int(mem)


def _atomic_json(path, obj):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


# ---- unit backends -------------------------------------------------------
class FakeUnitBackend:
    """TEST-ONLY. Records unit lifecycle to a spool dir and simulates the outcomes
    AC4/AC17 must exercise. Never selectable by a production (non-test_only) config."""
    name = "fake-unit"

    def __init__(self, spool):
        self.spool = spool
        os.makedirs(spool, exist_ok=True)

    def _path(self, unit):
        return os.path.join(self.spool, unit + ".json")

    def start(self, unit, spec, request_path, mode):
        p = self._path(unit)
        if os.path.exists(p):
            raise LaunchError("unit %s already known (unit-name lock)" % unit)
        with open(os.path.join(self.spool, "_starts.log"), "a") as lg:
            lg.write("%s %d %s\n" % (unit, int(time.time()), mode))
        rec = {"unit": unit, "backend": self.name, "request_path": request_path,
               "RuntimeMaxSec": runtime_max_sec(spec["timeout_cap"]), "MemoryMax": spec["memory"],
               "started_at": int(time.time())}
        if mode == "active":
            rec.update(ActiveState="active", Result="", SubState="running")
        elif mode == "clean-exit":
            rec.update(ActiveState="inactive", SubState="dead", Result="success",
                       ExecMainStatus=0, memory_peak=10 * 1024 * 1024)
        elif mode == "oom":
            rec.update(ActiveState="failed", SubState="failed", Result="oom-kill",
                       ExecMainStatus=137, systemd_result="CONSTRAINT_MEMCG",
                       memory_peak=runtime_bytes(spec["memory"]))
        elif mode == "fail-exit":
            rec.update(ActiveState="failed", SubState="failed", Result="exit-code",
                       ExecMainStatus=1, memory_peak=5 * 1024 * 1024)
        elif mode == "fast-exit-collected":
            return unit  # collected before observation: nothing left (show -> None)
        else:
            raise LaunchError("unknown fake mode %r" % mode)
        _atomic_json(p, rec)
        return unit


class SystemdBackend:
    name = "systemd"

    def start(self, unit, spec, request_path, mode):
        args = ["systemd-run", "--unit=%s" % unit, "--collect", "--uid=%s" % spec["user"],
                "--property=WorkingDirectory=%s" % spec["workdir"],
                "--property=MemoryMax=%s" % spec["memory"], "--property=MemorySwapMax=0",
                "--property=CPUQuota=%s" % spec["cpu"],
                "--property=RuntimeMaxSec=%d" % runtime_max_sec(spec["timeout_cap"]),
                "--property=NoNewPrivileges=yes", "--property=CapabilityBoundingSet=",
                "--property=ProtectSystem=strict", "--property=PrivateTmp=yes"]
        for r in spec.get("input_roots", []):
            args.append("--property=ReadOnlyPaths=%s" % r)
        for r in spec.get("output_roots", []):
            args.append("--property=ReadWritePaths=%s" % r)
        # the verified request is delivered as an immutable root-owned read-only file
        args.append("--property=ReadOnlyPaths=%s" % request_path)
        if spec.get("network", "none") == "none":
            args.append("--property=IPAddressDeny=any")
        args += ["--", spec["exec"], request_path]
        r = subprocess.run(args, capture_output=True)
        if r.returncode != 0:
            raise LaunchError("systemd-run failed: %s" % r.stderr.decode())
        return unit


def systemd_available():
    if not shutil.which("systemd-run"):
        return False
    if subprocess.run(["systemd-run", "--version"], capture_output=True).returncode != 0:
        return False
    return subprocess.run(["systemctl", "is-system-running"], capture_output=True).returncode == 0


def make_backend(cfg):
    """FAIL CLOSED (Pi #4). systemd requires a live bus; fake requires test_only."""
    kind = cfg.get("backend", "systemd")
    if kind == "systemd":
        if not systemd_available():
            raise LaunchError("backend=systemd but no live systemd bus — refusing (fail closed)")
        return SystemdBackend()
    if kind == "fake":
        if not cfg.get("test_only"):
            raise LaunchError("fake backend requires an explicit test_only config — refusing")
        return FakeUnitBackend(cfg["spool"])
    raise LaunchError("unknown backend %r" % kind)


def classify_terminal(show):
    """Map an observed unit (fake dict OR normalized systemctl show) to terminal
    fields (Pi #4: real memory.peak/runtime before AC9 can be pass_live)."""
    res = show.get("Result", "")
    mem = show.get("memory_peak")
    if mem is None and show.get("MemoryPeak"):
        try:
            mem = int(show["MemoryPeak"])
        except ValueError:
            mem = None
    rt = show.get("RuntimeMaxSec")
    if res == "success" and int(show.get("ExecMainStatus", 0) or 0) == 0:
        return "succeeded", None, mem, rt
    if res == "oom-kill" or show.get("systemd_result") == "CONSTRAINT_MEMCG":
        return "failed", "CONSTRAINT_MEMCG", mem, rt
    if res in ("timeout",):
        return "timeout", "RuntimeMaxSec", mem, rt
    return "failed", "exit_%s" % show.get("ExecMainStatus"), mem, rt


def unit_show(cfg, unit):
    """Observe a unit (unprivileged read). Normalizes systemd + fake into one shape."""
    if cfg.get("backend") == "systemd" and systemd_available():
        r = subprocess.run(["systemctl", "show", unit, "--no-page"], capture_output=True)
        if r.returncode != 0:
            return None
        d = dict(l.split("=", 1) for l in r.stdout.decode().splitlines() if "=" in l)
        if d.get("LoadState") == "not-found":
            return None
        return d
    p = os.path.join(cfg.get("spool", ""), unit + ".json")
    return json.load(open(p)) if os.path.exists(p) else None


# ---- config (root-owned; NOT caller-supplied) ----------------------------
def load_config():
    path = os.environ.get("CMP_LAUNCHER_CONFIG", DEFAULT_CONFIG)
    cfg = json.load(open(path))
    cfg.setdefault("queue_ref", "refs/heads/jobs/queue")
    for k in ("mirror", "allowed_signers", "registry", "ledger_db", "runtime_dir"):
        if k not in cfg:
            raise LaunchError("launcher config missing %s" % k)
    return cfg


# ---- the launcher --------------------------------------------------------
def launch(job_key, sha, rid, cfg):
    mirror, ref = cfg["mirror"], cfg["queue_ref"]
    tip = refs.ref_sha(mirror, ref)
    if tip is None or (tip != sha and not refs.is_ancestor(mirror, sha, tip)):
        raise LaunchError("commit %s not on %s" % (sha, ref))
    adds = refs.added_paths(mirror, sha)
    want = "requests/%s.json" % rid
    if len(adds) != 1 or adds[0] != ("A", want):
        raise LaunchError("commit does not add exactly %s" % want)
    raw = refs.read_blob(mirror, sha, want)                      # bytes by blob identity
    try:
        principal, fpr = refs.verify_commit(mirror, sha, cfg["allowed_signers"])  # AC14
    except ValueError as e:
        raise LaunchError("QUEUE-UNSIGNED: %s" % e)
    digest = refs.request_digest(raw)
    req = refs.validate_request(raw, rid)
    if req["job"] != job_key:
        raise LaunchError("job %r != launch key %r" % (req["job"], job_key))
    reg = registry.load(cfg["registry"])
    spec = registry.resolve(reg, job_key, req["job_version"])
    registry.verify_exec(spec)
    registry.validate_params(spec, req.get("params", {}))
    unit = "cmpjob-%s" % rid
    # durable at-most-once lock owned HERE (survives --collect); binds job_id->digest
    conn = ledger.connect(cfg["ledger_db"])
    res = ledger.try_intent(conn, digest, rid, unit, sha, principal, fpr)
    if res != "CREATED":
        return {"ok": False, "outcome": res, "request_digest": digest, "unit": unit,
                "principal": principal, "fingerprint": fpr}
    # deliver the exact verified request to the job as a root-owned read-only file
    os.makedirs(cfg["runtime_dir"], exist_ok=True)
    req_path = os.path.join(cfg["runtime_dir"], "%s.request.json" % unit)
    with open(req_path, "wb") as f:
        f.write(raw)
    os.chmod(req_path, 0o400)
    ledger.bind_input(conn, digest, req_path, hashlib.sha256(raw).hexdigest())
    backend = make_backend(cfg)          # fail-closed selection
    mode = os.environ.get("CMP_FAKE_MODE", "active") if backend.name == "fake-unit" else None
    backend.start(unit, spec, req_path, mode)
    return {"ok": True, "outcome": "CREATED", "unit": unit, "request_digest": digest,
            "principal": principal, "fingerprint": fpr, "backend": backend.name,
            "input_digest": hashlib.sha256(raw).hexdigest()}


def main(argv):
    if len(argv) != 4:
        print(json.dumps({"ok": False, "error": "usage: cmp-launch <key> <sha> <id>"}))
        return 2
    _, job_key, sha, rid = argv
    try:
        out = launch(job_key, sha, rid, load_config())
    except (LaunchError, refs.RequestError, registry.RegistryError, RuntimeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps(out))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
