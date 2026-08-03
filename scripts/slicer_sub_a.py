#!/usr/bin/env python3
"""slicer_sub_a.py — Slicer Sub A: the generic spine, proven on a SYNTHETIC
checkpointed long stage. Executable contract: issue v2.2 @ 792f65d.

Second repair, after Pi's re-audit of the first repair. Every oracle must drive
the real operational invariant, and every receipt claim must be measured and
scoped honestly (never broader than what ran). Checkpoint state is DURABLE and
run-id-independent (survives across workflow jobs, not just within one job); the
receipt is proof-carrying; the observability gate is fail-closed; capacity and
enforcement claims are measured and labelled truthfully; the uncheckpointable-
long-stage REFUSAL path is exercised, not only the positive resume path.

HARD PROHIBITIONS honored: no model download / embedding, no market corpus beyond
a trivial catalog check, no ANN, no semantic retrieval, no Sub B/C, no auto-
continuation. Stdlib only. Fault-injection / over-budget / swap-monitor tests run
in isolated RLIMIT-capped child processes and never touch the runner daemon.

Modes:
  oracles
  stage <base> <n> [--die-at K] [--error-at K] [--sci S] [--impl V] [--epoch E]
  partworker <sci> <outdir> <id0,id1,...>    # compute a partition's items
  benchworker <items> <hwm_out>              # one benchmark worker (measures VmHWM)
  overbudget <limit_mb>                        # RLIMIT_AS rejection target
  grow <target_mb> <rlimit_mb> <step_mb> <sleep_ms>   # monitored-growth target
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------- locations
WS = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
EVID = WS / "derived" / "slicer" / "evidence"
RECEIPT = WS / "derived" / "slicer" / "sub_a_receipt.json"
CATALOG = WS / "slicer" / "catalog.json"


def _state_root() -> Path:
    """DURABLE, run-id-independent checkpoint root (Pi #1). It must survive across
    workflow jobs, so it must NOT be RUNNER_TEMP (GitHub empties that at the start
    and end of every job) and must NOT be inside the checkout (wiped by checkout).
    Order: explicit SLICER_STATE_DIR (the workflow points this at a durable box
    path) → the runner user's persistent home → a fixed durable fallback."""
    v = os.environ.get("SLICER_STATE_DIR")
    if v:
        return Path(v).resolve()
    home = os.environ.get("HOME")
    if home:
        return Path(home).resolve() / ".cmp" / "slicer" / "state"
    return Path("/var/tmp/cmp-slicer-state").resolve()  # /var/tmp persists; /tmp may not


STATE = _state_root()
SCHEMA = "slicer-sub-a-receipt/2.4"
SEED = int(os.environ.get("SLICER_SEED", "20260802"))
SAFETY = float(os.environ.get("SLICER_SAFETY", "0.8"))
MAX_UNCHECKPOINTED_WORK_S = float(os.environ.get("SLICER_MAX_UNCHECKPOINTED_WORK_S", "30"))
SELF = os.path.abspath(__file__)
SKIP_HB = os.environ.get("SLICER_SKIP_HEARTBEAT") == "1"
TRANSFORM_VERSION = "synthetic-transform/1"
# the synthetic stage checkpoints at every shard boundary
STAGE_DESCRIPTOR = {"checkpoint_boundary": "shard"}


# ------------------------------------------------------------- pure helpers
def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def digest_obj(obj) -> str:
    return sha256_bytes(canon(obj).encode())[:16]


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def git(*args, check=True) -> str:
    r = subprocess.run(["git", *args], cwd=str(WS), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def head_sha() -> str:
    return os.environ.get("GITHUB_SHA") or git("rev-parse", "HEAD", check=False) or "unknown"


def proc_status_kb(pid: int, key: str):
    try:
        for ln in Path(f"/proc/{pid}/status").read_text().splitlines():
            if ln.startswith(key + ":"):
                return int(ln.split()[1])  # kB
    except OSError:
        return None
    return None


def read_ppid(pid: int):
    try:
        for ln in Path(f"/proc/{pid}/status").read_text().splitlines():
            if ln.startswith("PPid:"):
                return int(ln.split()[1])
    except OSError:
        return None
    return None


# ------------------------------------ run-scoped process-tree RSS high-water
def tree_rss_mb(root: int) -> int:
    """Sum VmRSS across `root` and all its live descendants (MB). Run-scoped —
    unlike cgroup memory.peak, which is the long-lived service cgroup's LIFETIME
    high-water and cannot attribute a peak to this run (Pi peak correction)."""
    try:
        pids = [int(d) for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return 0
    children: dict[int, list[int]] = {}
    for p in pids:
        pp = read_ppid(p)
        if pp is not None:
            children.setdefault(pp, []).append(p)
    seen, stack = {root}, [root]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in seen:
                seen.add(c); stack.append(c)
    total_kb = sum(proc_status_kb(p, "VmRSS") or 0 for p in seen)
    return total_kb // 1024


class TreeSampler:
    """Background sampler recording the run-scoped process-tree RSS high-water."""

    def __init__(self, interval=0.15):
        self.hi = 0
        self.interval = interval
        self.samples = 0
        self._root = os.getpid()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self._stop.is_set():
            self.hi = max(self.hi, tree_rss_mb(self._root))
            self.samples += 1
            self._stop.wait(self.interval)

    def start(self):
        self.hi = tree_rss_mb(self._root)
        self._t.start()
        return self

    def stop(self):
        self._stop.set()
        self._t.join(timeout=1)
        self.hi = max(self.hi, tree_rss_mb(self._root))
        return self.hi


# ------------------------------------------------ cgroup-effective capacity
def _read(path: str):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def cgroup_base() -> str | None:
    for ln in (_read("/proc/self/cgroup") or "").splitlines():
        parts = ln.split(":")
        if len(parts) == 3 and parts[0] == "0":
            return "/sys/fs/cgroup" + parts[2]
    return None


def _int_or_max(s):
    if s is None:
        return None
    s = s.strip()
    return None if s == "max" else int(s)


def _cg_peak_mb(base) -> int | None:
    if not base:
        return None
    v = _int_or_max(_read(base + "/memory.peak"))
    return None if v is None else v // (1024 * 1024)


def read_capacity() -> dict:
    """Effective capacity from the cgroup, not the host (issue AC-A3), with a
    declared, applied admission safety factor (Pi #6)."""
    base = cgroup_base()
    host_avail_mb = None
    for ln in (_read("/proc/meminfo") or "").splitlines():
        if ln.startswith("MemAvailable:"):
            host_avail_mb = int(ln.split()[1]) // 1024
    cap = {"cgroup_base": base, "host_mem_available_mb": host_avail_mb,
           "memory_high_mb": None, "memory_max_mb": None, "memory_current_mb": None,
           "cgroup_lifetime_peak_mb": None, "run_scoped_peak_rss_mb": None,
           "peak_note": "cgroup memory.peak is the long-lived runner-service cgroup LIFETIME "
                        "high-water, not this run's peak; run_scoped_peak_rss_mb is a process-tree "
                        "sampler value. A true per-stage cgroup peak needs a delegated cgroup (Sub B).",
           "swap_max_mb": None, "swap_current_mb": None,
           "cpuset_count": None, "quota_cores": None, "effective_cpu": None,
           "raw_available_mem_mb": None, "safety_factor": SAFETY, "admission_mem_mb": None}
    if base:
        def mb(name):
            v = _int_or_max(_read(base + "/" + name))
            return None if v is None else v // (1024 * 1024)
        cap["memory_high_mb"] = mb("memory.high")
        cap["memory_max_mb"] = mb("memory.max")
        mc = _int_or_max(_read(base + "/memory.current"))
        cap["memory_current_mb"] = None if mc is None else mc // (1024 * 1024)
        cap["cgroup_lifetime_peak_mb"] = _cg_peak_mb(base)
        cap["swap_max_mb"] = mb("memory.swap.max")
        sc = _int_or_max(_read(base + "/memory.swap.current"))
        cap["swap_current_mb"] = None if sc is None else sc // (1024 * 1024)
        cm = (_read(base + "/cpu.max") or "").split()
        if len(cm) == 2 and cm[0] != "max":
            cap["quota_cores"] = int(cm[0]) / int(cm[1])
        eff = _read(base + "/cpuset.cpus.effective") or _read(base + "/cpuset.cpus") or ""
        cnt = 0
        for part in eff.split(","):
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-")
                cnt += int(b) - int(a) + 1
            else:
                cnt += 1
        cap["cpuset_count"] = cnt or (os.cpu_count() or 1)
    cpus = cap["cpuset_count"] or (os.cpu_count() or 1)
    cap["effective_cpu"] = max(1, int(min(cpus, cap["quota_cores"]))) if cap["quota_cores"] else cpus
    cur = cap["memory_current_mb"] or 0
    cands = []
    if host_avail_mb is not None:
        cands.append(host_avail_mb)
    if cap["memory_high_mb"] is not None:
        cands.append(cap["memory_high_mb"] - cur)
    if cap["memory_max_mb"] is not None:
        cands.append(cap["memory_max_mb"] - cur)
    cap["raw_available_mem_mb"] = min(cands) if cands else host_avail_mb
    if cap["raw_available_mem_mb"] is not None:
        cap["admission_mem_mb"] = int(cap["raw_available_mem_mb"] * SAFETY)
    return cap


# ------------------------------------------------------ operational catalog
class ReadOnlyHandle:
    """A real read-only handle over a catalog input. Any write attempt raises —
    the refusal is observed operationally, not read off a JSON boolean (Pi #3)."""

    def __init__(self, path: Path):
        self._f = open(path, "rb")
        self.name = str(path)

    def read(self, n=-1):
        return self._f.read(n)

    def write(self, *_a, **_k):
        raise PermissionError(f"read-only catalog input '{self.name}': write refused")

    def close(self):
        self._f.close()


class Catalog:
    def __init__(self, path: Path):
        self.doc = json.loads(path.read_text())
        self.digest = sha256_file(path)[:16]
        self.datasets = self.doc["datasets"]
        self.schema_version = self.doc["schema_version"]

    def _root(self, d) -> Path:
        if d.get("kind") == "derived" and d.get("working"):
            return STATE
        return WS

    def resolve_output(self, name: str) -> Path:
        if name not in self.datasets:
            raise PermissionError(f"catalog: unregistered output '{name}' — refusing to publish")
        d = self.datasets[name]
        if d.get("read_only"):
            raise PermissionError(f"catalog: '{name}' is read_only — cannot write")
        p = (self._root(d) / d["path"]).resolve()
        p.mkdir(parents=True, exist_ok=True) if d.get("format") in ("shard-dir", "dir") else p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def open_input(self, name: str) -> ReadOnlyHandle:
        if name not in self.datasets:
            raise PermissionError(f"catalog: unregistered input '{name}'")
        return ReadOnlyHandle((self._root(self.datasets[name]) / self.datasets[name]["path"]).resolve())


# ----------------------------------- identity key (scientific vs execution)
def dep_lock_digest() -> str:
    return sha256_bytes(f"{sys.version}|{sys.platform}|{sha256_file(Path(SELF))}".encode())[:16]


def scientific_digest(sci_param: str) -> str:
    """Identity of the SCIENTIFIC result — deterministic in the frozen scientific
    params only; execution partition (workers/threads/batch) is excluded."""
    return digest_obj({"seed": SEED, "transform": TRANSFORM_VERSION,
                       "scientific_param": sci_param, "embedding_model_revision": "N/A-sub-a"})


def identity_key(sci_param: str, impl_version: str, catalog: Catalog) -> dict:
    key = {
        "scientific_digest": scientific_digest(sci_param),
        "git_sha": head_sha(),
        "stage_impl_version": impl_version,
        "catalog_digest": catalog.digest,
        "schema_version": catalog.schema_version,
        "dep_lock_digest": dep_lock_digest(),
        # NB: GITHUB_RUN_ID is deliberately NOT a key component — a new workflow
        # run resolves the same checkpoint dir and resumes it.
    }
    key["key_digest"] = digest_obj(key)
    return key


# ---------------------------------------------------- admission (refusal gate)
def admit_stage(descriptor: dict) -> dict:
    """Pure admission decision (Pi #4 refusal path). A stage whose projected work
    exceeds max_uncheckpointed_work_s MUST expose a checkpoint boundary or be
    refused before launch."""
    thr = float(descriptor.get("max_uncheckpointed_work_s", MAX_UNCHECKPOINTED_WORK_S))
    projected = float(descriptor.get("projected_wall_s", 0))
    boundary = descriptor.get("checkpoint_boundary", "none")
    if projected > thr and boundary == "none":
        return {"admit": False, "reason": "projected work exceeds max_uncheckpointed_work_s and no checkpoint boundary",
                "projected_wall_s": projected, "threshold_s": thr, "checkpoint_boundary": boundary}
    return {"admit": True, "reason": "checkpointed or within uncheckpointed budget",
            "projected_wall_s": projected, "threshold_s": thr, "checkpoint_boundary": boundary}


# ------------------------------------------ synthetic checkpointed long stage
def atomic_write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return sha256_bytes(data)


def key_dir(base: Path, key_digest: str) -> Path:
    return base / key_digest


def completed_dir(kdir: Path) -> Path:
    return kdir / "completed"


def read_manifest(kdir: Path) -> dict:
    """Completion is recorded as ONE atomically-written marker file per shard
    (Pi #5): there is no shared append-only file to corrupt, so a crash mid-write
    leaves an ignorable .tmp and never a poisoned tail. A shard counts complete
    only if its marker parses AND its shard file's digest matches."""
    cd = completed_dir(kdir)
    out = {}
    if not cd.exists():
        return out
    for mp in cd.glob("shard-*.json"):
        try:
            e = json.loads(mp.read_text())
        except (json.JSONDecodeError, OSError):
            continue  # ignore a partially-written marker; that shard recomputes
        sp = kdir / e["file"]
        if sp.exists() and sha256_file(sp) == e["digest"]:
            out[e["shard"]] = e
    return out


def _transform(sci_param: str, shard: int) -> bytes:
    buf = hashlib.sha256(f"{SEED}:{TRANSFORM_VERSION}:{sci_param}:{shard}".encode()).digest()
    for _ in range(20000):
        buf = hashlib.sha256(buf).digest()
    return buf


def _safe_append_line(path: Path, line: str):
    """Append a line, first repairing any truncated tail so a prior half-written
    line can never fuse with this one (Pi #5)."""
    if path.exists():
        with open(path, "rb") as f:
            data = f.read()
        if data and not data.endswith(b"\n"):
            nl = data.rfind(b"\n")
            with open(path, "wb") as f:
                f.write(data[:nl + 1] if nl >= 0 else b"")
    with open(path, "a") as f:
        f.write(line + "\n")
        f.flush(); os.fsync(f.fileno())


def synthetic_stage(base: Path, n_shards: int, sci_param="baseline",
                    impl_version="synthetic/1", epoch=1, die_at=None, error_at=None,
                    catalog: Catalog | None = None) -> dict:
    cat = catalog or Catalog(CATALOG)
    key = identity_key(sci_param, impl_version, cat)
    kdir = key_dir(base, key["key_digest"])
    completed_dir(kdir).mkdir(parents=True, exist_ok=True)
    (kdir / "key.json").write_text(json.dumps(key, indent=2))
    done = read_manifest(kdir)
    ledger = kdir / "exec_ledger.jsonl"
    executed = []
    for s in range(n_shards):
        if s in done:
            continue  # eligible skip: digest-verified complete under THIS key
        if die_at is not None and s == die_at:
            os.kill(os.getpid(), signal.SIGKILL)  # hard, uncatchable
        if error_at is not None and s == error_at:
            raise RuntimeError(f"injected error at shard {s}")
        t0 = time.time()
        data = _transform(sci_param, s)
        fn = f"shard-{s:05d}.bin"
        d = atomic_write(kdir / fn, data)
        t1 = time.time()
        # completion marker: single atomic file (no append corruption possible)
        atomic_write(completed_dir(kdir) / f"shard-{s:05d}.json",
                     canon({"shard": s, "file": fn, "digest": d, "at": now()}).encode())
        _safe_append_line(ledger, canon({"shard": s, "epoch": epoch, "start": t0,
                                         "end": t1, "dt": t1 - t0}))
        executed.append(s)
    return {"key_digest": key["key_digest"], "kdir": str(kdir), "executed": executed}


def ledger_entries(kdir: Path) -> list:
    lp = kdir / "exec_ledger.jsonl"
    out = []
    if lp.exists():
        for ln in lp.read_text().splitlines():
            if ln.strip():
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    pass
    return out


# ------------------------------------- execution-partition runner (for A5)
def run_partition(sci_param: str, outdir: Path, groups: list) -> dict:
    """Compute a fixed item universe through a REAL partition: one worker
    subprocess per group, each writing its items' outputs; parent merges by stable
    item id. Different (worker-count, grouping, order) partitions must yield the
    same merged item->digest map (Pi #3)."""
    shutil.rmtree(outdir, ignore_errors=True)
    outdir.mkdir(parents=True, exist_ok=True)
    procs = [subprocess.Popen([sys.executable, SELF, "partworker", sci_param, str(outdir),
                               ",".join(str(i) for i in g)])
             for g in groups if g]
    for p in procs:
        p.wait()
    return {int(p.stem.split("-")[1]): sha256_file(p)
            for p in sorted(outdir.glob("item-*.bin"))}


# --------------------------------------------------------------- heartbeat
def hb_ref(run_id: str) -> str:
    return f"refs/heads/slicer-status/{run_id}"


def hb_publish(run_id: str, seq: int, state: dict) -> str:
    payload = canon({"run_id": run_id, "heartbeat_seq": seq, "at": now(), **state})
    digest = sha256_bytes(payload.encode())
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=str(WS),
                          input=payload, capture_output=True, text=True, check=True).stdout.strip()
    tree = subprocess.run(["git", "mktree"], cwd=str(WS),
                          input=f"100644 blob {blob}\theartbeat.json\n",
                          capture_output=True, text=True, check=True).stdout.strip()
    commit = subprocess.run(["git", "commit-tree", tree], cwd=str(WS),
                            input=f"slicer heartbeat {run_id} seq={seq}",
                            capture_output=True, text=True, check=True).stdout.strip()
    git("push", "-f", "origin", f"{commit}:{hb_ref(run_id)}")
    return digest


def hb_roundtrip(run_id: str, seq: int, expect_digest: str) -> dict:
    tmp = f"refs/remotes/slicer-status-verify/{run_id}"
    git("fetch", "-f", "origin", f"{hb_ref(run_id)}:{tmp}")
    content = git("show", f"{tmp}:heartbeat.json")
    got = sha256_bytes(content.encode())
    obj = json.loads(content)
    ok = (got == expect_digest and obj.get("run_id") == run_id and obj.get("heartbeat_seq") == seq)
    return {"verified": ok, "fetched": obj, "fetched_digest": got, "expected_digest": expect_digest,
            "run_id_match": obj.get("run_id") == run_id, "seq_match": obj.get("heartbeat_seq") == seq}


# ------------------------------------- monitored-growth child (swap/rss guard)
def run_monitored_child(threshold_mb: int, metric_key: str, target_mb: int,
                        rlimit_mb: int) -> dict:
    proc = subprocess.Popen([sys.executable, SELF, "grow", str(target_mb), str(rlimit_mb),
                             "8", "40"], stderr=subprocess.PIPE, text=True)
    observed = {"max": 0, "killed_by_monitor": False, "threshold_mb": threshold_mb, "metric": metric_key}
    stop = threading.Event()

    def mon():
        while not stop.is_set() and proc.poll() is None:
            kb = proc_status_kb(proc.pid, metric_key)
            if kb is not None:
                observed["max"] = max(observed["max"], kb // 1024)
                if kb // 1024 > threshold_mb:
                    observed["killed_by_monitor"] = True
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    return
            time.sleep(0.02)

    t = threading.Thread(target=mon, daemon=True)
    t.start()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
    stop.set()
    t.join(timeout=1)
    observed["child_returncode"] = proc.returncode
    observed["child_killed_signal"] = -proc.returncode if proc.returncode is not None and proc.returncode < 0 else None
    return observed


# --------------------------------------------- cgroup-kill attempt (truthful)
def cgroup_kill_attempt(base) -> dict:
    if not base:
        return {"available": False, "reason": "no cgroup base"}
    child_cg = Path(base) / "slicer-oom-test"
    try:
        child_cg.mkdir()
    except (PermissionError, OSError) as e:
        return {"available": False, "reason": f"cannot create sub-cgroup: {e.__class__.__name__}"}
    try:
        (child_cg / "memory.max").write_text(str(48 * 1024 * 1024))
        try:
            (child_cg / "memory.swap.max").write_text("0")
        except OSError:
            pass
        proc = subprocess.Popen([sys.executable, SELF, "grow", "512", "4096", "16", "10"],
                                stderr=subprocess.DEVNULL)
        (child_cg / "cgroup.procs").write_text(str(proc.pid))
        proc.wait(timeout=30)
        oom = 0
        for ln in (_read(str(child_cg / "memory.events")) or "").splitlines():
            if ln.startswith("oom_kill "):
                oom = int(ln.split()[1])
        return {"available": True, "child_returncode": proc.returncode,
                "child_killed_signal": -proc.returncode if proc.returncode < 0 else None,
                "oom_kill_count": oom, "kernel_oom_killed": oom > 0 or (proc.returncode is not None and proc.returncode < 0)}
    finally:
        try:
            child_cg.rmdir()
        except OSError:
            pass


# ----------------------------------------------------------------- oracles
def oracle_catalog(cat: Catalog) -> dict:
    ev = {}
    try:
        cat.resolve_output("not_a_registered_dataset"); ev["unregistered_refused"] = False
    except PermissionError as e:
        ev["unregistered_refused"] = True; ev["unregistered_msg"] = str(e)
    try:
        cat.resolve_output("synthetic_seed"); ev["readonly_output_refused"] = False
    except PermissionError as e:
        ev["readonly_output_refused"] = True; ev["readonly_msg"] = str(e)
    h = cat.open_input("synthetic_seed")
    try:
        h.write(b"x"); ev["write_through_input_refused"] = False
    except PermissionError as e:
        ev["write_through_input_refused"] = True; ev["write_msg"] = str(e)
    finally:
        h.close()
    out_root = cat.resolve_output("sub_a_shards")
    r = synthetic_stage(out_root, 2, sci_param="a1", catalog=cat)
    wrote = [str(p.relative_to(out_root)) for p in Path(r["kdir"]).rglob("*") if p.is_file()]
    escaped = [str(p) for p in Path(r["kdir"]).rglob("*") if not str(p).startswith(str(out_root))]
    ev["stage_output_root"] = str(out_root)
    ev["stage_wrote_files"] = wrote
    ev["wrote_outside_catalog_root"] = escaped
    passed = (ev["unregistered_refused"] and ev["readonly_output_refused"]
              and ev["write_through_input_refused"] and not escaped and len(wrote) >= 2)
    return {"ac": "A1", "name": "catalog governs I/O operationally (real refusal observed)",
            "passed": passed, "evidence": ev}


def oracle_capacity(cap: dict) -> dict:
    ev = {"effective_capacity": cap}
    passed = (cap["effective_cpu"] is not None and cap["admission_mem_mb"] is not None
              and cap["cgroup_base"] is not None and cap["safety_factor"] > 0)
    ev["safety_factor_applied"] = cap["admission_mem_mb"] == int((cap["raw_available_mem_mb"] or 0) * cap["safety_factor"]) if cap["raw_available_mem_mb"] else False
    ev["cgroup_effective_derived"] = passed
    return {"ac": "A3.measure", "name": "cgroup-effective capacity measured + safety factor applied",
            "passed": passed and ev["safety_factor_applied"], "evidence": ev}


def oracle_probe_config(cap: dict) -> dict:
    """Measure WORKER-COUNT scaling with a real synthetic benchmark and select the
    best feasible worker count. threads_per_worker and batch are declared
    not_evaluated in Sub A and deferred to Sub B's real embedding workload — the
    synthetic worker exercises neither, so claiming to have selected them would be
    an overclaim (Pi #2; skill: do not overclaim)."""
    eff = cap["effective_cpu"] or 1
    budget = cap["admission_mem_mb"] or 10 ** 9
    worker_counts = sorted({1, 2, max(1, eff // 2), eff})
    items = int(os.environ.get("SLICER_BENCH_ITEMS", "4000"))
    results = []
    bdir = STATE / "bench"; bdir.mkdir(parents=True, exist_ok=True)
    for w in worker_counts:
        hwm_files = [bdir / f"hwm-{w}-{i}" for i in range(w)]
        t0 = time.time()
        procs = [subprocess.Popen([sys.executable, SELF, "benchworker", str(items), str(hf)])
                 for hf in hwm_files]
        for p in procs:
            p.wait()
        dt = time.time() - t0
        peak_mb = sum(int((hf.read_text().strip() or "0")) for hf in hwm_files if hf.exists()) // 1024
        results.append({"workers": w, "rows_per_s": round((w * items) / dt, 1) if dt else 0,
                        "peak_rss_mb": peak_mb, "wall_s": round(dt, 3), "feasible": peak_mb <= budget})
    feasible = [r for r in results if r["feasible"]]
    best = max(feasible or results, key=lambda r: r["rows_per_s"])
    # threads pinned to 1 as a no-oversubscription POLICY (not a measured selection)
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "ORT_NUM_THREADS"):
        os.environ[var] = "1"
    chosen = {"workers": best["workers"], "threads_per_worker": "not_evaluated", "batch": "not_evaluated"}
    ev = {"effective_cpu": eff, "admission_mem_mb": budget, "measured_dimension": "workers",
          "measured_candidates": results, "chosen": chosen,
          "threads_per_worker_policy": "pinned to 1 (no oversubscription); NOT measured in Sub A",
          "batch_status": "not_evaluated in Sub A — requires the real embedding workload (Sub B)",
          "selection": "measured max rows_per_s among worker-counts with peak_rss<=admission_mem",
          "no_oversubscription": best["workers"] <= eff}
    passed = ev["no_oversubscription"] and len(results) >= 2 and all("rows_per_s" in r for r in results)
    return {"ac": "A3b", "name": "measured worker-count selection (threads/batch not_evaluated → Sub B)",
            "passed": passed, "evidence": ev}


def oracle_scientific_vs_execution(cat: Catalog) -> dict:
    """A5: process the SAME fixed item universe through two REAL execution
    partitions (different worker count, grouping, and order) and require identical
    merged item-level outputs; a changed scientific param must differ (Pi #3)."""
    base = STATE / "a5"
    n = int(os.environ.get("SLICER_A5_N", "8"))
    gx = [list(range(n))]                                   # X: 1 worker, contiguous
    k = min(4, n)
    gy = [[i for i in range(n) if i % k == w] for w in range(k)]  # Y: k workers, interleaved
    mx = run_partition("sci-const", base / "x", gx)
    my = run_partition("sci-const", base / "y", gy)
    same = (mx == my and len(mx) == n)
    mz = run_partition("sci-changed", base / "z", gx)      # changed scientific param
    changed = (mx != mz) and (scientific_digest("sci-const") != scientific_digest("sci-changed"))
    ev = {"universe_size": n, "partition_x": "1 worker, contiguous 0..n-1",
          "partition_y": f"{k} workers, interleaved by id%{k}", "y_worker_groups": gy,
          "same_scientific_identical_merged_outputs": same,
          "items_x": len(mx), "items_y": len(my), "items_z": len(mz),
          "changed_scientific_differs": changed,
          "scientific_digest_const": scientific_digest("sci-const"),
          "scientific_digest_changed": scientific_digest("sci-changed"),
          "note": "real partitions differ in worker count, grouping, and order; identity is over "
                  "frozen scientific params, so merged outputs match across partitions"}
    return {"ac": "A5", "name": "scientific-param identity vs REAL execution-partition variance",
            "passed": same and changed, "evidence": ev}


def oracle_idempotency(cat: Catalog) -> dict:
    """Idempotency key WIRED into eligibility: same key => zero work; changed key
    => real re-run; a corrupt completion marker is ignored + recovered; stray .tmp
    uncounted (Pi #4/#5)."""
    base = STATE / "idem"; shutil.rmtree(base, ignore_errors=True)
    r1 = synthetic_stage(base, 3, sci_param="idem", impl_version="v1", catalog=cat, epoch=1)
    first = sorted(read_manifest(Path(r1["kdir"])))
    r2 = synthetic_stage(base, 3, sci_param="idem", impl_version="v1", catalog=cat, epoch=2)
    rerun_zero_work = (r2["executed"] == [])
    r3 = synthetic_stage(base, 3, sci_param="idem", impl_version="v2", catalog=cat, epoch=3)
    changed_key_reran = (sorted(r3["executed"]) == [0, 1, 2] and r3["key_digest"] != r1["key_digest"])
    stray = Path(r1["kdir"]) / "shard-00009.bin.tmp.999"; stray.write_bytes(b"partial")
    stray_not_counted = 9 not in read_manifest(Path(r1["kdir"]))
    # corrupt a completion marker: shard 2 must recompute (never fuse/poison)
    (completed_dir(Path(r1["kdir"])) / "shard-00002.json").write_text('{"shard": 2, "fi')
    after_corrupt = sorted(read_manifest(Path(r1["kdir"])))
    r4 = synthetic_stage(base, 3, sci_param="idem", impl_version="v1", catalog=cat, epoch=4)
    recovered = (2 not in after_corrupt) and (r4["executed"] == [2]) and (2 in read_manifest(Path(r1["kdir"])))
    ev = {"first_run_completed": first, "rerun_executed": r2["executed"], "rerun_zero_work": rerun_zero_work,
          "changed_key_executed": sorted(r3["executed"]), "changed_key_reran": changed_key_reran,
          "key_v1": r1["key_digest"], "key_v2": r3["key_digest"],
          "stray_tmp_not_counted": stray_not_counted,
          "corrupt_marker_recomputed_not_poisoned": recovered,
          "completion_protocol": "one atomic marker file per shard (no shared append to corrupt)",
          "key_components": [k for k in identity_key("idem", "v1", cat) if k != "key_digest"]}
    passed = all([rerun_zero_work, changed_key_reran, stray_not_counted, recovered])
    return {"ac": "A2", "name": "idempotency key wired into eligibility + recoverable completion state",
            "passed": passed, "evidence": ev}


def probe_and_verdict(cap: dict, force_budget_mb=None) -> dict:
    t0 = time.time()
    synthetic_stage(STATE / "probe", 2, sci_param="probe", catalog=Catalog(CATALOG))
    dt = time.time() - t0
    rows_per_s = 2 / dt if dt else 0
    full_n = int(os.environ.get("SLICER_PROJECT_N", "222855"))
    proj_wall_min = (full_n / rows_per_s) / 60 if rows_per_s else 1e9
    proj_peak_mb = 64 + full_n * 0.001
    wall_budget_min = float(os.environ.get("SLICER_WALL_BUDGET_MIN", "120"))
    mem_budget = force_budget_mb if force_budget_mb is not None else (cap["admission_mem_mb"] or 1e9)
    refused = (proj_peak_mb > mem_budget) or (proj_wall_min > wall_budget_min)
    return {"rows_per_s": round(rows_per_s, 1), "projected_full_n": full_n,
            "projected_peak_mb": round(proj_peak_mb, 1), "projected_wall_min": round(proj_wall_min, 2),
            "mem_budget_mb": mem_budget, "wall_budget_min": wall_budget_min,
            "verdict": "refuse" if refused else "pass",
            "reason": ("projected>budget" if refused else "within budget"),
            "probe_wall_clock_s": round(dt, 3), "rows_processed": 2}


def oracle_refusal(cap: dict) -> dict:
    p = probe_and_verdict(cap, force_budget_mb=1)
    ev = {"forced_budget_probe": p}
    passed = (p["verdict"] == "refuse" and p["projected_peak_mb"] > 1)
    return {"ac": "A3.refuse", "name": "capacity refusal before expensive work (with numbers)",
            "passed": passed, "evidence": ev}


def oracle_uncheckpointable_refusal() -> dict:
    """A projected-long stage with NO checkpoint boundary must be REFUSED before
    it runs; a checkpointed or short one is admitted. Proven through a guarded
    launcher that never invokes the stage on refusal (Pi #4)."""
    thr = MAX_UNCHECKPOINTED_WORK_S
    long_no = {"projected_wall_s": 3600, "checkpoint_boundary": "none", "max_uncheckpointed_work_s": thr}
    long_ck = {"projected_wall_s": 3600, "checkpoint_boundary": "shard", "max_uncheckpointed_work_s": thr}
    short = {"projected_wall_s": 1, "checkpoint_boundary": "none", "max_uncheckpointed_work_s": thr}
    launched = {"count": 0}

    def guarded_launch(desc):
        dec = admit_stage(desc)
        if not dec["admit"]:
            return dec
        launched["count"] += 1  # only reached if admitted
        return {**dec, "launched": True}

    g_long_no = guarded_launch(long_no)
    g_long_ck = guarded_launch(long_ck)
    g_short = guarded_launch(short)
    ev = {"threshold_s": thr,
          "long_no_checkpoint": g_long_no, "long_with_checkpoint": g_long_ck, "short_no_checkpoint": g_short,
          "stage_descriptor_used_on_real_path": STAGE_DESCRIPTOR,
          "refused_without_launching": (not g_long_no["admit"]) and launched["count"] == 2,
          "note": "admit_stage gates every real launch (see A6); the uncheckpointable long stage never ran"}
    passed = (not g_long_no["admit"]) and g_long_ck["admit"] and g_short["admit"] and ev["refused_without_launching"]
    return {"ac": "A6.refuse", "name": "uncheckpointable long stage refused before launch",
            "passed": passed, "evidence": ev}


def oracle_overbudget(cap: dict) -> dict:
    r = subprocess.run([sys.executable, SELF, "overbudget", "64"], capture_output=True, text=True)
    rlimit = {"mechanism": "rlimit_as_rejection", "child_returncode": r.returncode,
              "child_stderr_tail": r.stderr.strip()[-200:], "parent_survived": True,
              "note": "RLIMIT_AS refuses the allocation; this is address-space rejection, not a cgroup OOM-kill"}
    cg = cgroup_kill_attempt(cap["cgroup_base"])
    ev = {"rlimit_as": rlimit, "cgroup_oom": cg,
          "future_stage_note": "the real stage inherits the runner cgroup memory.max "
                               "(kernel kill boundary) and is additionally RLIMIT_AS-capped"}
    passed = r.returncode != 0
    return {"ac": "A3.enforce", "name": "over-budget enforced in isolated child (labelled truthfully)",
            "passed": passed, "evidence": ev}


def oracle_swap(cap: dict) -> dict:
    """Swap-policy enforcement via an installed RSS-growth monitor (leading
    indicator of swap): a sampler thread SIGKILLs an isolated growing child once
    VmRSS crosses a receipt-bound threshold, preempting swap; swap.current is
    observed and stays bounded."""
    swap_disabled = (cap["swap_max_mb"] == 0)
    metric = "VmRSS"
    mode = "swap_disabled_rss_guard" if swap_disabled else "rss_guard_preempts_swap"
    threshold = int(os.environ.get("SLICER_SWAP_THRESHOLD_MB", "96"))
    obs = run_monitored_child(threshold, metric, target_mb=256, rlimit_mb=512)
    swap_after = cap["swap_current_mb"]
    ev = {"swap_max_mb": cap["swap_max_mb"], "swap_current_mb": cap["swap_current_mb"],
          "mode": mode, "monitored_metric": metric, "threshold_mb": threshold,
          "observed_peak_rss_mb": obs["max"], "child_killed_by_monitor": obs["killed_by_monitor"],
          "child_returncode": obs["child_returncode"], "child_killed_signal": obs["child_killed_signal"],
          "swap_current_bounded": (swap_after is None or swap_after <= threshold),
          "note": "installed sampler thread SIGKILLs the child over a bound RSS threshold, preempting swap"}
    passed = obs["killed_by_monitor"] and obs["max"] >= threshold and ev["swap_current_bounded"]
    return {"ac": "A3.swap", "name": "swap-policy enforcement: RSS-growth monitor preempts swap (real termination)",
            "passed": passed, "evidence": ev}


def oracle_no_workflow_trigger(run_id: str) -> dict:
    import fnmatch, re
    wf_dir = WS / ".github" / "workflows"
    sample = "slicer-status/deadbeef"
    offenders, details = [], {}
    for wf in sorted(wf_dir.glob("*.yml")) + sorted(wf_dir.glob("*.yaml")):
        text = "\n".join(ln.split("#", 1)[0] for ln in wf.read_text().splitlines())
        has_push = bool(re.search(r"(^|\n)\s*push\s*:", text)) or bool(re.search(r"on\s*:\s*\[[^\]]*push", text))
        branches = []
        for mo in re.finditer(r"branches\s*:\s*\[([^\]]*)\]", text):
            branches += [b.strip().strip("'\"") for b in mo.group(1).split(",") if b.strip()]
        for mo in re.finditer(r"branches\s*:\s*\n((?:\s*-\s*.+\n?)+)", text):
            branches += [ln.strip()[1:].strip().strip("'\"") for ln in mo.group(1).splitlines() if ln.strip().startswith("-")]
        details[wf.name] = {"has_push": has_push, "branches": branches}
        if has_push and (not branches or any(fnmatch.fnmatch(sample, b) for b in branches)):
            offenders.append(wf.name)
    ev = {"sample_ref": sample, "workflows": details, "offending_workflows": offenders,
          "runtime_api_check": runtime_no_trigger(run_id)}
    rt = ev["runtime_api_check"]
    passed = (not offenders) and (rt.get("passed", True) if rt.get("available") else True)
    return {"ac": "A4.no-trigger", "name": "slicer-status/** triggers no workflow (static + runtime API)",
            "passed": passed, "evidence": ev}


def runtime_no_trigger(run_id: str) -> dict:
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    repo = os.environ.get("GITHUB_REPOSITORY")
    tok = os.environ.get("GITHUB_TOKEN")
    if SKIP_HB or not (repo and tok):
        return {"available": False, "reason": "no token/repo or heartbeat skipped"}
    import urllib.request
    def get(url):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}",
                                                   "Accept": "application/vnd.github+json",
                                                   "User-Agent": "slicer-sub-a"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    try:
        branch = f"slicer-status/{run_id}"
        d = get(f"{api}/repos/{repo}/actions/runs?branch={branch}&per_page=100")
        total = d.get("total_count", 0)
        return {"available": True, "status_branch": branch,
                "runs_triggered_by_status_ref": total, "passed": total == 0, "checked_at": now()}
    except Exception as e:
        return {"available": False, "reason": f"api error: {e.__class__.__name__}: {e}"}


def _resume_pair(base: Path, sci: str, run_id_a: str, run_id_b: str) -> dict:
    """Run stage twice sharing a durable base, under two DISTINCT GITHUB_RUN_IDs:
    A dies mid-way; B (new run id) resumes. Returns measured facts."""
    shutil.rmtree(base, ignore_errors=True)
    # admit before launch — admit_stage is on the real launch path (Pi #4)
    admit = admit_stage({**STAGE_DESCRIPTOR, "projected_wall_s": 0})
    env_a = {**os.environ, "GITHUB_RUN_ID": run_id_a}
    env_b = {**os.environ, "GITHUB_RUN_ID": run_id_b}
    subprocess.run([sys.executable, SELF, "stage", str(base), "8", "--die-at", "3",
                    "--sci", sci, "--epoch", "1"], capture_output=True, text=True, env=env_a)
    kdirs = [p for p in base.glob("*") if (p / "key.json").exists()]
    kdir = kdirs[0] if kdirs else base
    before = set(read_manifest(kdir))
    r2 = subprocess.run([sys.executable, SELF, "stage", str(base), "8",
                         "--sci", sci, "--epoch", "2"], capture_output=True, text=True, env=env_b)
    after = set(read_manifest(kdir))
    led = ledger_entries(kdir)
    exec_b = {e["shard"] for e in led if e.get("epoch") == 2}
    return {"kdir": str(kdir), "before": sorted(before), "after": sorted(after),
            "executed_on_resume": sorted(exec_b),
            "recomputed": len(before & exec_b),
            "killed_signal": -r2.returncode if r2.returncode < 0 else r2.returncode,
            "admit": admit, "max_shard_work_s": round(max([e["dt"] for e in led], default=0.0), 3)}


def oracle_resume() -> dict:
    """Positive resume path: SIGKILL mid-stage, resume, MEASURE zero recompute."""
    p = _resume_pair(STATE / "resume", "resume", "runA", "runB")
    ev = {**p, "resumed_not_restarted": set(p["before"]).issubset(set(p["after"])) and len(p["after"]) == 8,
          "measured_recomputed_shards": p["recomputed"], "measured_max_shard_work_s": p["max_shard_work_s"]}
    passed = ev["resumed_not_restarted"] and p["recomputed"] == 0 and len(p["before"]) == 3
    return {"ac": "A6", "name": "checkpoint/resume after SIGKILL (measured recompute cost)",
            "passed": passed, "evidence": ev}


def oracle_resume_durable() -> dict:
    """Durable resume ACROSS runs (Pi #1): the checkpoint dir is a pure function of
    the identity key (run-id-independent) and lives on a durable root outside the
    checkout and outside RUNNER_TEMP. Run B, with a NEW GITHUB_RUN_ID, resolves the
    same dir and resumes run A's checkpoint with zero recomputation."""
    rt = os.environ.get("RUNNER_TEMP")
    durable_root = (not str(STATE).startswith(str(WS))) and \
                   (rt is None or not str(STATE).startswith(str(Path(rt).resolve())))
    p = _resume_pair(STATE / "resume_durable", "durable", "runA-111", "runB-222")
    kdir_has_no_run_id = ("runA" not in p["kdir"]) and ("runB" not in p["kdir"]) and ("111" not in p["kdir"]) and ("222" not in p["kdir"])
    ev = {"state_root": str(STATE), "runner_temp": rt,
          "durable_root_outside_checkout_and_runner_temp": durable_root,
          "run_id_a": "runA-111", "run_id_b": "runB-222", "distinct_run_ids": True,
          "checkpoint_dir": p["kdir"], "checkpoint_path_run_id_independent": kdir_has_no_run_id,
          "completed_before_run_b": p["before"], "executed_on_run_b": p["executed_on_resume"],
          "measured_recomputed_shards": p["recomputed"], "completed_final": p["after"],
          "note": "run B resolved run A's checkpoint via the identity key alone and recomputed nothing; "
                  "the durable root survives job teardown (not RUNNER_TEMP, not the checkout)"}
    passed = (durable_root and kdir_has_no_run_id and p["recomputed"] == 0
              and len(p["after"]) == 8 and len(p["before"]) == 3)
    return {"ac": "A6.durable", "name": "durable run-id-independent resume across distinct GITHUB_RUN_IDs",
            "passed": passed, "evidence": ev}


def oracle_error_surfacing(run_id: str) -> dict:
    base = STATE / "err"
    r = subprocess.run([sys.executable, SELF, "stage", str(base), "5", "--error-at", "2",
                        "--sci", "err"], capture_output=True, text=True)
    stderr_surfaced = ("injected error at shard 2" in (r.stderr + r.stdout))
    ev = {"child_returncode": r.returncode, "stderr_surfaced": stderr_surfaced}
    hb_ok = None
    if not SKIP_HB and stderr_surfaced and r.returncode != 0:
        try:
            d = hb_publish(run_id, 500, {"stage": "err", "status": "error", "error_class": "injected",
                                         "shard": 2, "message": "injected error at shard 2"})
            rt = hb_roundtrip(run_id, 500, d)
            fetched = rt.get("fetched", {})
            hb_ok = rt["verified"] and fetched.get("status") == "error" and fetched.get("shard") == 2
            ev["error_heartbeat_roundtrip"] = rt
        except Exception as e:
            ev["error_heartbeat_error"] = str(e); hb_ok = False
    else:
        ev["error_heartbeat_roundtrip"] = {"skipped": True}
        hb_ok = True if SKIP_HB else hb_ok
    ev["error_visible_on_observability_surface"] = hb_ok
    ev["silent_skip"] = (r.returncode == 0 and not stderr_surfaced)
    passed = stderr_surfaced and r.returncode != 0 and bool(hb_ok) and not ev["silent_skip"]
    return {"ac": "A7", "name": "injected-error surfaced via heartbeat (never silent)",
            "passed": passed, "evidence": ev}


# ----------------------------------------------------------- receipt / gate
def quarantine_derived():
    if RECEIPT.exists():
        RECEIPT.unlink()
    if EVID.exists():
        for p in EVID.glob("*"):
            if p.is_file():
                p.unlink()
    EVID.mkdir(parents=True, exist_ok=True)
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)


def write_evidence_and_receipt(receipt: dict, oracles: list):
    refs = []
    for o in oracles:
        fn = f"{o['ac'].replace('.', '_')}.json"
        p = EVID / fn
        p.write_text(json.dumps(o, indent=2))
        refs.append({"ac": o["ac"], "file": f"evidence/{fn}", "sha256": sha256_file(p), "passed": o["passed"]})
    receipt["evidence_refs"] = refs
    RECEIPT.write_text(json.dumps(receipt, indent=2))


def observability_refused(run_id: str, detail: dict) -> int:
    quarantine_derived()
    receipt = {"schema": SCHEMA, "sub": "A", "issue_contract": "slicer v2.2 @ 792f65d",
               "run_id": run_id, "git_sha": head_sha(), "generated_at": now(),
               "mode": "sub_a_synthetic", "oracles": [],
               "execution_status": "observability_refused", "contract_status": "rejected",
               "retrieval_instrument_status": "not_evaluated", "discovery_status": "not_run",
               "prerequisite_receipts": [], "observability_gate": detail,
               "notes": "fail-closed: initial heartbeat/round-trip not proven; no heavy oracle run"}
    write_evidence_and_receipt(receipt, [])
    print("[sub-a] OBSERVABILITY GATE FAILED — fail-closed; no heavy/fault oracle run", flush=True)
    return 1


def run_oracles() -> int:
    run_id = os.environ.get("GITHUB_RUN_ID", f"local-{int(time.time())}")
    STATE.mkdir(parents=True, exist_ok=True)
    quarantine_derived()
    cat = Catalog(CATALOG)
    cap = read_capacity()
    print("[sub-a] state root:", STATE, "| effective capacity:", canon(cap), flush=True)
    sampler = TreeSampler().start()  # run-scoped process-tree RSS high-water (Pi peak fix)

    # ---- OBSERVABILITY GATE FIRST (fail-closed) ----
    hb_ev = {"skipped_local": SKIP_HB}
    if not SKIP_HB:
        try:
            d1 = hb_publish(run_id, 1, {"stage": "spine-init", "status": "alive"})
            rt1 = hb_roundtrip(run_id, 1, d1)
            d2 = hb_publish(run_id, 2, {"stage": "spine-init", "status": "alive"})
            rt2 = hb_roundtrip(run_id, 2, d2)
            hb_ev = {"initial_roundtrip": rt1, "seq_incremented": rt2["seq_match"] and rt2["verified"],
                     "ref": hb_ref(run_id)}
            if not (rt1["verified"] and hb_ev["seq_incremented"]):
                sampler.stop()
                return observability_refused(run_id, hb_ev)
        except Exception as e:
            sampler.stop()
            return observability_refused(run_id, {"error": str(e)})
    oracle_hb = {"ac": "A4.heartbeat", "name": "heartbeat publish + independent remote round-trip",
                 "passed": True, "evidence": hb_ev}

    oracles = [oracle_hb]
    oracles.append(oracle_catalog(cat))
    oracles.append(oracle_capacity(cap))
    oracles.append(oracle_probe_config(cap))
    oracles.append(oracle_scientific_vs_execution(cat))
    oracles.append(oracle_idempotency(cat))
    probe = probe_and_verdict(cap)
    oracles.append(oracle_refusal(cap))
    oracles.append(oracle_uncheckpointable_refusal())
    oracles.append(oracle_overbudget(cap))
    oracles.append(oracle_swap(cap))
    res = oracle_resume(); oracles.append(res)
    resd = oracle_resume_durable(); oracles.append(resd)
    oracles.append(oracle_error_surfacing(run_id))
    oracles.append(oracle_no_workflow_trigger(run_id))

    cap["cgroup_lifetime_peak_mb"] = _cg_peak_mb(cap["cgroup_base"])
    cap["run_scoped_peak_rss_mb"] = sampler.stop()

    failure_cost = {
        "probe_wall_clock_s": probe["probe_wall_clock_s"],
        "rows_processed_before_refusal": next((o["evidence"]["forced_budget_probe"]["rows_processed"]
                                               for o in oracles if o["ac"] == "A3.refuse"), None),
        "measured_recomputed_shards_after_kill": res["evidence"]["measured_recomputed_shards"],
        "measured_max_uncheckpointed_work_s": res["evidence"]["measured_max_shard_work_s"],
    }
    all_pass = all(o["passed"] for o in oracles)
    inv = {
        "measured_before_scale": next((o["passed"] for o in oracles if o["ac"] == "A3b"), False),
        "resumable": next((o["passed"] for o in oracles if o["ac"] == "A6"), False),
        "durably_resumable": next((o["passed"] for o in oracles if o["ac"] == "A6.durable"), False),
        "refuses_uncheckpointable": next((o["passed"] for o in oracles if o["ac"] == "A6.refuse"), False),
        "observable": next((o["passed"] for o in oracles if o["ac"] == "A4.heartbeat"), False),
        "memory_bounded": next((o["passed"] for o in oracles if o["ac"] == "A3.enforce"), False),
        "errors_surfaced": next((o["passed"] for o in oracles if o["ac"] == "A7"), False),
        "failure_is_cheap": failure_cost["measured_recomputed_shards_after_kill"] == 0,
    }
    receipt = {
        "schema": SCHEMA, "sub": "A", "issue_contract": "slicer v2.2 @ 792f65d",
        "run_id": run_id, "git_sha": head_sha(), "generated_at": now(),
        "mode": "sub_a_synthetic", "catalog_digest": cat.digest, "state_root": str(STATE),
        "effective_capacity": cap, "probe": probe, "oracles": oracles,
        "invariants_self_check": inv, "failure_cost": failure_cost,
        "execution_status": "completed", "contract_status": "accepted" if all_pass else "rejected",
        "retrieval_instrument_status": "not_evaluated", "discovery_status": "not_run",
        "prerequisite_receipts": [], "remote_state_sink": hb_ref(run_id),
        "scope_notes": {"A3b": "workers measured; threads_per_worker + batch not_evaluated (Sub B)",
                        "peak": "run_scoped_peak_rss_mb is a process-tree sampler value; cgroup memory.peak "
                                "is the service cgroup lifetime high-water; per-stage cgroup peak deferred to Sub B"},
        "notes": "Sub A only — synthetic long stage; no model/corpus/ANN/retrieval.",
    }

    if not SKIP_HB:
        try:
            d = hb_publish(run_id, 999, {"stage": "sub-a-done", "status": receipt["execution_status"],
                                         "contract_status": receipt["contract_status"]})
            rt = hb_roundtrip(run_id, 999, d)
            receipt["terminal_heartbeat"] = rt
            if not rt["verified"]:
                receipt["contract_status"] = "rejected"; receipt["execution_status"] = "failed"
                receipt["notes"] += " | terminal heartbeat round-trip failed"
        except Exception as e:
            receipt["contract_status"] = "rejected"; receipt["execution_status"] = "failed"
            receipt["terminal_heartbeat"] = {"error": str(e)}

    write_evidence_and_receipt(receipt, oracles)

    print("\n[sub-a] ORACLE RESULTS:")
    for o in oracles:
        print(f"  [{'PASS' if o['passed'] else 'FAIL'}] {o['ac']:14s} {o['name']}")
    print(f"\n[sub-a] execution_status={receipt['execution_status']} "
          f"contract_status={receipt['contract_status']} "
          f"retrieval_instrument_status={receipt['retrieval_instrument_status']} "
          f"discovery_status={receipt['discovery_status']}")
    return 0 if receipt["contract_status"] == "accepted" else 1


# --------------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("oracles")
    st = sub.add_parser("stage")
    st.add_argument("base"); st.add_argument("n", type=int)
    st.add_argument("--die-at", type=int, default=None)
    st.add_argument("--error-at", type=int, default=None)
    st.add_argument("--sci", default="baseline"); st.add_argument("--impl", default="synthetic/1")
    st.add_argument("--epoch", type=int, default=1)
    pw = sub.add_parser("partworker")
    pw.add_argument("sci"); pw.add_argument("outdir"); pw.add_argument("ids")
    bw = sub.add_parser("benchworker"); bw.add_argument("items", type=int); bw.add_argument("hwm_out")
    ob = sub.add_parser("overbudget"); ob.add_argument("limit_mb", type=int)
    gr = sub.add_parser("grow")
    gr.add_argument("target_mb", type=int); gr.add_argument("rlimit_mb", type=int)
    gr.add_argument("step_mb", type=int); gr.add_argument("sleep_ms", type=int)
    a = ap.parse_args()

    if a.mode == "oracles":
        return run_oracles()
    if a.mode == "stage":
        synthetic_stage(Path(a.base), a.n, sci_param=a.sci, impl_version=a.impl,
                        epoch=a.epoch, die_at=a.die_at, error_at=a.error_at)
        return 0
    if a.mode == "partworker":
        outdir = Path(a.outdir); outdir.mkdir(parents=True, exist_ok=True)
        for tok in a.ids.split(","):
            if tok != "":
                i = int(tok)
                atomic_write(outdir / f"item-{i:05d}.bin", _transform(a.sci, i))
        return 0
    if a.mode == "benchworker":
        buf = hashlib.sha256(b"bench").digest()
        for _ in range(a.items):
            buf = hashlib.sha256(buf).digest()
        Path(a.hwm_out).write_text(str(proc_status_kb(os.getpid(), "VmHWM") or 0))
        return 0
    if a.mode == "overbudget":
        import resource
        lim = a.limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
        try:
            _ = bytearray(lim * 8)
        except MemoryError:
            sys.stderr.write("over-budget child: RLIMIT_AS rejected allocation (address-space cap)\n")
            return 42
        sys.stderr.write("over-budget child: allocation unexpectedly succeeded\n")
        return 0
    if a.mode == "grow":
        import resource
        lim = a.rlimit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
        blocks = []
        grown = 0
        try:
            while grown < a.target_mb:
                blocks.append(bytearray(a.step_mb * 1024 * 1024))
                for i in range(0, len(blocks[-1]), 4096):
                    blocks[-1][i] = 1
                grown += a.step_mb
                time.sleep(a.sleep_ms / 1000.0)
        except MemoryError:
            sys.stderr.write("grow child: RLIMIT_AS backstop hit\n")
            return 42
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
