"""cmp_launch.py — privileged unit-launch mechanics + pluggable backend (§4.1).

This module has NO configuration loading and NO environment trust (Pi re-audit #2):
it is a set of pure functions called ONLY in-process by the privileged owner
(cmp_admitd), which supplies an already-verified spec and already-trusted paths.
There is no CLI and no CMP_* env a caller could use to swap the trust store or the
backend. Backend selection fails closed; unit observation uses the same trusted
backend identity the privileged owner launched with.
"""
import os, json, subprocess, tempfile, shutil, time, pwd


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


# ---- verified-request delivery (Pi re-audit #3) --------------------------
def deliver_request(runtime_dir, unit, raw, run_user):
    """Create the verified request as an immutable file the job user can READ.

    O_CREAT|O_EXCL|O_NOFOLLOW (cannot follow or overwrite a pre-existing/symlinked
    path), fsync(file)+fsync(dir), then chgrp to the job user's primary group + mode
    0440 (root-owned, group-readable by `cmpjob`). ReadOnlyPaths only affects
    mutability, so read perm is granted explicitly here. Returns (path, note)."""
    os.makedirs(runtime_dir, mode=0o755, exist_ok=True)
    path = os.path.join(runtime_dir, "%s.request.json" % unit)
    if os.path.lexists(path):
        os.unlink(path)                      # our own prior artifact; O_EXCL guards races below
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o400)
    try:
        os.write(fd, raw)
        os.fsync(fd)
        note = "root-owned 0440"
        try:
            gid = pwd.getpwnam(run_user).pw_gid
            os.fchown(fd, 0, gid); os.fchmod(fd, 0o440)
            note = "root:%s 0440 (group-readable by job user)" % run_user
        except KeyError:
            os.fchmod(fd, 0o440)             # fixture: job user absent; production chowns root:cmpjob
            note = "root-owned 0440 (job user %r absent in fixture; prod chowns root:%s)" % (run_user, run_user)
    finally:
        os.close(fd)
    dfd = os.open(runtime_dir, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return path, note


def cleanup_request(path):
    try:
        if path and os.path.lexists(path):
            os.unlink(path)
    except OSError:
        pass


# ---- backends ------------------------------------------------------------
class FakeUnitBackend:
    """TEST-ONLY. Records unit lifecycle to a spool dir and simulates outcomes."""
    name = "fake-unit"

    def __init__(self, spool):
        self.spool = spool
        os.makedirs(spool, exist_ok=True)

    def _p(self, unit):
        return os.path.join(self.spool, unit + ".json")

    def start(self, unit, spec, request_path, mode):
        if os.path.exists(self._p(unit)):
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
            return unit                      # collected before observation
        elif mode == "start-fail":
            raise LaunchError("backend start refused (simulated)")
        else:
            raise LaunchError("unknown fake mode %r" % mode)
        _atomic_json(self._p(unit), rec)
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


def make_backend(kind, spool, test_only):
    """FAIL CLOSED (Pi #4). systemd requires a live bus; fake requires test_only."""
    if kind == "systemd":
        if not systemd_available():
            raise LaunchError("backend=systemd but no live systemd bus — refusing (fail closed)")
        return SystemdBackend()
    if kind == "fake":
        if not test_only:
            raise LaunchError("fake backend requires an explicit test_only config — refusing")
        return FakeUnitBackend(spool)
    raise LaunchError("unknown backend %r" % kind)


def unit_show(kind, spool, unit):
    """Observe a unit with the TRUSTED backend identity (Pi #2) — never a
    caller-supplied backend/spool."""
    if kind == "systemd" and systemd_available():
        r = subprocess.run(["systemctl", "show", unit, "--no-page"], capture_output=True)
        if r.returncode != 0:
            return None
        d = dict(l.split("=", 1) for l in r.stdout.decode().splitlines() if "=" in l)
        return None if d.get("LoadState") == "not-found" else d
    p = os.path.join(spool, unit + ".json")
    return json.load(open(p)) if os.path.exists(p) else None


def classify_terminal(show):
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
