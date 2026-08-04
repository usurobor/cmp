"""cmp_launch.py — the hardened root launcher (§4.2) + pluggable unit backend (§4.1).

Invoked ONLY as:  cmp-launch <registry-key> <queue-commit-sha> <request-id>

It trusts NO poller-supplied path or bytes. From a FIXED root-owned bare mirror it
independently: reads requests/<id>.json by commit/blob identity at the sha, verifies
the queue commit's SSH signature against the root-owned allowed_signers, recomputes
the exact-byte request_digest, validates schema/id-grammar/job/version, checks the
commit adds exactly this one request, re-resolves the registry + re-verifies
exec_sha256, then launches via a pluggable unit backend. A parser-compromised poller
cannot forge the signature (AC14) because verification happens here, from the mirror,
not from anything the poller hands over.

Root-owned configuration comes from the environment (not from poller argv beyond the
three identity tokens):
  CMP_MIRROR, CMP_ALLOWED_SIGNERS, CMP_REGISTRY, CMP_QUEUE_REF,
  CMP_BACKEND (fake|systemd), CMP_SPOOL, CMP_FAKE_MODE
"""
import os, sys, json, subprocess, tempfile, shutil, time
import refs
import registry


# ---- unit backends -------------------------------------------------------
def runtime_max_sec(cap):
    m = {"h": 3600, "m": 60, "s": 1}
    cap = str(cap)
    return int(cap[:-1]) * m[cap[-1]] if cap and cap[-1] in m else int(cap)


class LaunchError(Exception):
    pass


class FakeUnitBackend:
    """Records unit lifecycle to a spool dir. Simulates the four outcomes the doc
    requires AC4/AC17 to exercise: active, clean-exit, fast-exit-then-collected
    (nothing left to observe, §3.5), and OOM CONSTRAINT_MEMCG."""
    name = "fake-unit"

    def __init__(self, spool):
        self.spool = spool
        os.makedirs(spool, exist_ok=True)

    def _path(self, unit):
        return os.path.join(self.spool, unit + ".json")

    def start(self, unit, spec, mode):
        p = self._path(unit)
        # unit-name lock (§3.5 second lock): refuse a start against a live/known unit
        if os.path.exists(p):
            raise LaunchError("unit %s already known (unit-name lock)" % unit)
        # append-only start log: every real start attempt is recorded so tests can
        # prove reconciliation NEVER relaunches (AC4/AC17).
        with open(os.path.join(self.spool, "_starts.log"), "a") as lg:
            lg.write("%s %d %s\n" % (unit, int(time.time()), mode))
        rec = {"unit": unit, "backend": self.name,
               "RuntimeMaxSec": runtime_max_sec(spec["timeout_cap"]),
               "MemoryMax": spec["memory"], "started_at": int(time.time())}
        if mode == "active":
            rec.update(ActiveState="active", Result="", SubState="running")
        elif mode == "clean-exit":
            rec.update(ActiveState="inactive", SubState="dead", Result="success",
                       ExecMainStatus=0, memory_peak=10 * 1024 * 1024)
        elif mode == "oom":
            rec.update(ActiveState="failed", SubState="failed", Result="oom-kill",
                       ExecMainStatus=137, oom_kill=True,
                       systemd_result="CONSTRAINT_MEMCG",
                       memory_peak=runtime_bytes(spec["memory"]))
        elif mode == "fail-exit":
            rec.update(ActiveState="failed", SubState="failed", Result="exit-code",
                       ExecMainStatus=1, memory_peak=5 * 1024 * 1024)
        elif mode == "fast-exit-collected":
            # --collect reaped the transient unit before the ledger advanced: there is
            # nothing left to observe. Do NOT write a record (show() -> None).
            return unit
        else:
            raise LaunchError("unknown fake mode %r" % mode)
        _atomic_json(p, rec)
        return unit


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


def systemd_available():
    if not shutil.which("systemd-run"):
        return False
    r = subprocess.run(["systemd-run", "--version"], capture_output=True)
    if r.returncode != 0:
        return False
    # a start needs a live bus; probe cheaply
    return subprocess.run(["systemctl", "is-system-running"], capture_output=True).returncode in (0,)


class SystemdBackend:
    name = "systemd"

    def __init__(self, spool=None):
        pass

    def start(self, unit, spec, mode):
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
        if spec.get("network", "none") == "none":
            args.append("--property=IPAddressDeny=any")
        args += ["--", spec["exec"]]
        r = subprocess.run(args, capture_output=True)
        if r.returncode != 0:
            raise LaunchError("systemd-run failed: %s" % r.stderr.decode())
        return unit


def make_backend(kind, spool):
    if kind == "systemd" and systemd_available():
        return SystemdBackend(spool)
    return FakeUnitBackend(spool)


def unit_show(spool, unit, backend="fake"):
    """Observe a unit's state (poller-side, unprivileged read)."""
    if backend == "systemd" and systemd_available():
        r = subprocess.run(["systemctl", "show", unit, "--no-page"], capture_output=True)
        if r.returncode != 0:
            return None
        d = dict(l.split("=", 1) for l in r.stdout.decode().splitlines() if "=" in l)
        if d.get("LoadState") == "not-found":
            return None
        return d
    p = os.path.join(spool, unit + ".json")
    if not os.path.exists(p):
        return None
    return json.load(open(p))


# ---- the launcher --------------------------------------------------------
def launch(job_key, sha, rid, cfg):
    """Independently re-establish provenance from the fixed mirror, then launch.
    Returns the started unit name. Raises LaunchError on any rejection."""
    mirror = cfg["mirror"]
    ref = cfg.get("queue_ref", "refs/heads/jobs/queue")
    # 0. commit must be on the queue ref and add exactly this one request (§4.2.3)
    if not refs.is_ancestor(mirror, sha, refs.ref_sha(mirror, ref)) and refs.ref_sha(mirror, ref) != sha:
        raise LaunchError("commit %s not on %s" % (sha, ref))
    adds = refs.added_paths(mirror, sha)
    want = "requests/%s.json" % rid
    if len(adds) != 1 or adds[0] != ("A", want):
        raise LaunchError("commit does not add exactly %s" % want)
    # 1. read the request bytes by blob identity at the sha (no poller path/bytes)
    raw = refs.read_blob(mirror, sha, want)
    # 2. independently verify the queue commit's signature (AC14)
    try:
        principal, fpr = refs.verify_commit(mirror, sha, cfg["allowed_signers"])
    except ValueError as e:
        raise LaunchError("QUEUE-UNSIGNED: %s" % e)
    # 3. recompute digest + validate schema/id/job/version
    digest = refs.request_digest(raw)
    req = refs.validate_request(raw, rid)      # raises RequestError
    if req["job"] != job_key:
        raise LaunchError("job %r != launch key %r" % (req["job"], job_key))
    # 4. re-resolve registry + re-verify exec_sha256 + params
    reg = registry.load(cfg["registry"])
    spec = registry.resolve(reg, job_key, req["job_version"])
    registry.verify_exec(spec)
    registry.validate_params(spec, req.get("params", {}))
    # 5. launch
    backend = make_backend(cfg.get("backend", "fake"), cfg.get("spool"))
    unit = "cmpjob-%s" % rid
    mode = cfg.get("fake_mode", os.environ.get("CMP_FAKE_MODE", "active"))
    backend.start(unit, spec, mode)
    return unit, digest, principal, fpr, backend.name


def cfg_from_env():
    return {"mirror": os.environ["CMP_MIRROR"],
            "allowed_signers": os.environ["CMP_ALLOWED_SIGNERS"],
            "registry": os.environ["CMP_REGISTRY"],
            "queue_ref": os.environ.get("CMP_QUEUE_REF", "refs/heads/jobs/queue"),
            "backend": os.environ.get("CMP_BACKEND", "fake"),
            "spool": os.environ.get("CMP_SPOOL"),
            "fake_mode": os.environ.get("CMP_FAKE_MODE", "active")}


def main(argv):
    if len(argv) != 4:
        print(json.dumps({"ok": False, "error": "usage: cmp-launch <key> <sha> <id>"}))
        return 2
    _, job_key, sha, rid = argv
    try:
        unit, digest, principal, fpr, backend = launch(job_key, sha, rid, cfg_from_env())
    except (LaunchError, refs.RequestError, registry.RegistryError, RuntimeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1
    print(json.dumps({"ok": True, "unit": unit, "request_digest": digest,
                      "principal": principal, "fingerprint": fpr, "backend": backend}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
