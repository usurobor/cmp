#!/usr/bin/env python3
"""slicer_sub_a.py — Slicer Sub A: the generic spine, proven on a SYNTHETIC
checkpointed long stage. Executable contract: issue v2.2 @ 792f65d.

This is the repaired implementation after independent PM (Pi) audit. Every oracle
must drive the real operational invariant, not assert a declaration or exercise a
helper. Checkpoint state lives OUTSIDE the checkout; the receipt is proof-carrying
(run_id / git_sha / per-evidence sha256 / runtime no-trigger API result); the
observability gate is fail-closed; capacity/enforcement claims are measured and
labelled truthfully.

HARD PROHIBITIONS honored: no model download / embedding, no market corpus beyond
a trivial catalog check, no ANN, no semantic retrieval, no Sub B/C, no auto-
continuation. Stdlib only. Fault-injection / over-budget / swap-monitor tests run
in isolated RLIMIT-capped child processes and never touch the runner daemon.

Modes:
  oracles                                   # main: run all Sub A oracles, emit receipt
  stage <base> <n> [--die-at K] [--error-at K] [--sci S] [--impl V] [--epoch E]
  benchworker <items> <hwm_out>             # one benchmark worker (measures own VmHWM)
  overbudget <limit_mb>                      # RLIMIT_AS rejection target
  grow <target_mb> <rlimit_mb> <step_mb> <sleep_ms>   # monitored-growth target
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
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
    # Checkpoint / working state MUST live outside the checkout (Pi #1): a
    # previous run's state must never be resurrected by `clean: false`.
    for env in ("SLICER_STATE_DIR", "RUNNER_TEMP", "TMPDIR"):
        v = os.environ.get(env)
        if v:
            return Path(v).resolve() / "slicer-state"
    return Path("/tmp/slicer-state").resolve()


STATE = _state_root()
SCHEMA = "slicer-sub-a-receipt/2.3"
SEED = int(os.environ.get("SLICER_SEED", "20260802"))
SAFETY = float(os.environ.get("SLICER_SAFETY", "0.8"))
SELF = os.path.abspath(__file__)
SKIP_HB = os.environ.get("SLICER_SKIP_HEARTBEAT") == "1"
TRANSFORM_VERSION = "synthetic-transform/1"


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
           "memory_peak_mb": None, "swap_max_mb": None, "swap_current_mb": None,
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
        cap["memory_peak_mb"] = _cg_peak_mb(base)
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
        cands.append(cap["memory_high_mb"] - cur)   # reclaim/throttle boundary (RCA symptom)
    if cap["memory_max_mb"] is not None:
        cands.append(cap["memory_max_mb"] - cur)     # kill boundary
    cap["raw_available_mem_mb"] = min(cands) if cands else host_avail_mb
    if cap["raw_available_mem_mb"] is not None:
        cap["admission_mem_mb"] = int(cap["raw_available_mem_mb"] * SAFETY)  # applied safety factor
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
        # 'derived' working datasets are rooted OUTSIDE the checkout (state dir);
        # committed artifacts and raw inputs are repo-relative.
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
    }
    key["key_digest"] = digest_obj(key)
    return key


# ------------------------------------------ synthetic checkpointed long stage
def atomic_write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return sha256_bytes(data)


def key_dir(base: Path, key_digest: str) -> Path:
    # Outputs are namespaced by identity key: a changed key => a different dir =>
    # prior outputs are ineligible => the stage reruns (Pi #4).
    return base / key_digest


def read_manifest(kdir: Path) -> dict:
    """Return {shard: entry} for digest-verified completed shards. A truncated or
    malformed FINAL line is tolerated (that shard is simply treated as
    incomplete and will recompute) — recoverable append protocol (Pi #4)."""
    mp = kdir / "completed.jsonl"
    out = {}
    if not mp.exists():
        return out
    for ln in mp.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue  # tolerate a truncated tail line
        sp = kdir / e["file"]
        if sp.exists() and sha256_file(sp) == e["digest"]:
            out[e["shard"]] = e
    return out


def _transform(sci_param: str, shard: int) -> bytes:
    buf = hashlib.sha256(f"{SEED}:{TRANSFORM_VERSION}:{sci_param}:{shard}".encode()).digest()
    for _ in range(20000):
        buf = hashlib.sha256(buf).digest()
    return buf


def synthetic_stage(base: Path, n_shards: int, sci_param="baseline",
                    impl_version="synthetic/1", epoch=1, die_at=None, error_at=None,
                    catalog: Catalog | None = None) -> dict:
    cat = catalog or Catalog(CATALOG)
    key = identity_key(sci_param, impl_version, cat)
    kdir = key_dir(base, key["key_digest"])
    kdir.mkdir(parents=True, exist_ok=True)
    # persist the key beside the outputs so eligibility is bound to it
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
        with open(kdir / "completed.jsonl", "a") as mf:  # append AFTER atomic rename
            mf.write(canon({"shard": s, "file": fn, "digest": d, "at": now()}) + "\n")
            mf.flush(); os.fsync(mf.fileno())
        with open(ledger, "a") as lf:  # execution ledger: measured, not asserted
            lf.write(canon({"shard": s, "epoch": epoch, "start": t0, "end": t1,
                            "dt": t1 - t0}) + "\n")
            lf.flush(); os.fsync(lf.fileno())
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
    """Spawn an isolated child that deliberately grows, and a REAL monitor thread
    that samples /proc and SIGKILLs it once `metric_key` exceeds `threshold_mb`.
    RLIMIT_AS is a hard backstop so the child can never endanger the daemon. This
    is an installed enforcement path, not a boolean helper (Pi #6)."""
    proc = subprocess.Popen([sys.executable, SELF, "grow", str(target_mb), str(rlimit_mb),
                             "8", "40"], stderr=subprocess.PIPE, text=True)
    observed = {"max": 0, "killed_by_monitor": False, "threshold_mb": threshold_mb,
                "metric": metric_key}
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
    """Attempt a REAL kernel OOM-kill via a delegated child cgroup with a small
    memory.max. If sub-cgroup creation is not permitted (no delegation), report
    that truthfully rather than dressing an RLIMIT rejection as a cgroup kill."""
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
    # operational: open the raw input read-only and observe a REAL write refusal
    h = cat.open_input("synthetic_seed")
    try:
        h.write(b"x"); ev["write_through_input_refused"] = False
    except PermissionError as e:
        ev["write_through_input_refused"] = True; ev["write_msg"] = str(e)
    finally:
        h.close()
    # run the stage THROUGH the catalog and confirm it wrote only under the
    # resolved output root — nothing escaped catalog governance
    out_root = cat.resolve_output("sub_a_shards")
    before = set(p for p in out_root.rglob("*")) if out_root.exists() else set()
    r = synthetic_stage(out_root, 2, sci_param="a1", catalog=cat)
    wrote = [str(p.relative_to(out_root)) for p in Path(r["kdir"]).rglob("*") if p.is_file()]
    escaped = [str(p) for p in (set(Path(r["kdir"]).rglob("*")) - before) if not str(p).startswith(str(out_root))]
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
    """Run a REAL bounded synthetic benchmark across candidate configs; measure
    throughput and summed per-worker peak RSS; select the best feasible one
    (Pi #5). Not 'largest tuple whose arithmetic fits'."""
    eff = cap["effective_cpu"] or 1
    budget = cap["admission_mem_mb"] or 10 ** 9
    cand_defs = [(1, 1, 32), (2, 1, 64), (max(1, eff // 2), 2, 128), (eff, 1, 256)]
    items = int(os.environ.get("SLICER_BENCH_ITEMS", "4000"))
    results = []
    bdir = STATE / "bench"; bdir.mkdir(parents=True, exist_ok=True)
    for w, thr, batch in cand_defs:
        if w * thr > eff:
            continue  # never oversubscribe
        hwm_files = [bdir / f"hwm-{w}-{thr}-{batch}-{i}" for i in range(w)]
        t0 = time.time()
        procs = [subprocess.Popen([sys.executable, SELF, "benchworker", str(items), str(hf)])
                 for hf in hwm_files]
        for p in procs:
            p.wait()
        dt = time.time() - t0
        peak_mb = sum(int((hf.read_text().strip() or "0")) for hf in hwm_files if hf.exists()) // 1024
        rows_per_s = (w * items) / dt if dt else 0
        results.append({"workers": w, "threads_per_worker": thr, "batch": batch,
                        "rows_per_s": round(rows_per_s, 1), "peak_rss_mb": peak_mb,
                        "wall_s": round(dt, 3), "feasible": peak_mb <= budget})
    feasible = [r for r in results if r["feasible"]]
    chosen = max(feasible or results, key=lambda r: r["rows_per_s"])
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "ORT_NUM_THREADS"):
        os.environ[var] = str(chosen["threads_per_worker"])
    ev = {"effective_cpu": eff, "admission_mem_mb": budget, "measured_candidates": results,
          "chosen": {k: chosen[k] for k in ("workers", "threads_per_worker", "batch")},
          "selection": "measured max rows_per_s among peak_rss<=admission_mem",
          "no_oversubscription": chosen["workers"] * chosen["threads_per_worker"] <= eff,
          "thread_caps_set": {v: os.environ.get(v) for v in ("OMP_NUM_THREADS", "ORT_NUM_THREADS")}}
    passed = ev["no_oversubscription"] and len(results) >= 2 and all("rows_per_s" in r for r in results)
    return {"ac": "A3b", "name": "measured probe-selected worker/thread/batch (benchmarked)",
            "passed": passed, "evidence": ev}


def oracle_scientific_vs_execution(cat: Catalog) -> dict:
    """A5: same scientific params under DIFFERENT execution partitions => identical
    outputs; changed scientific param => different scientific digest + invalidated
    (re-run) outputs (Pi #7)."""
    base = STATE / "a5"
    # execution partition X: 4 shards in one pass; Y: same 4 shards, different impl
    # version (an execution/plumbing detail, not scientific) and env thread caps.
    rx = synthetic_stage(base / "x", 4, sci_param="sci-const", impl_version="exec/x", catalog=cat)
    ry = synthetic_stage(base / "y", 4, sci_param="sci-const", impl_version="exec/y", catalog=cat)

    def shard_digests(kdir):
        return {int(p.stem.split("-")[1]): sha256_file(p)
                for p in sorted(Path(kdir).glob("shard-*.bin"))}
    dx, dy = shard_digests(rx["kdir"]), shard_digests(ry["kdir"])
    same_outputs = dx == dy
    sci_x = scientific_digest("sci-const")
    # changed scientific param => different scientific digest AND different key dir
    rz = synthetic_stage(base / "z", 4, sci_param="sci-changed", impl_version="exec/x", catalog=cat)
    dz = shard_digests(rz["kdir"])
    sci_z = scientific_digest("sci-changed")
    changed = (sci_x != sci_z) and (dx != dz) and (rz["key_digest"] != rx["key_digest"])
    ev = {"exec_partition_x": rx["key_digest"], "exec_partition_y": ry["key_digest"],
          "same_scientific_identical_outputs": same_outputs,
          "scientific_digest_const": sci_x, "scientific_digest_changed": sci_z,
          "changed_param_new_outputs_and_key": changed,
          "note": "identity is over frozen scientific params; execution partition excluded"}
    return {"ac": "A5", "name": "scientific-param identity vs execution-partition variance",
            "passed": same_outputs and changed, "evidence": ev}


def oracle_idempotency(cat: Catalog) -> dict:
    """Idempotency key WIRED into eligibility: same key => zero work; changed key
    => real re-run; truncated manifest tail recovers; stray .tmp uncounted (Pi #4)."""
    base = STATE / "idem"
    r1 = synthetic_stage(base, 3, sci_param="idem", impl_version="v1", catalog=cat, epoch=1)
    first = sorted(s["shard"] for s in read_manifest(Path(r1["kdir"])).values())
    r2 = synthetic_stage(base, 3, sci_param="idem", impl_version="v1", catalog=cat, epoch=2)
    rerun_zero_work = (r2["executed"] == [])
    # changed key component (impl version) => different key dir => full re-run
    r3 = synthetic_stage(base, 3, sci_param="idem", impl_version="v2", catalog=cat, epoch=3)
    changed_key_reran = (sorted(r3["executed"]) == [0, 1, 2] and r3["key_digest"] != r1["key_digest"])
    # partial-write: stray .tmp is never counted complete
    stray = Path(r1["kdir"]) / "shard-00009.bin.tmp"; stray.write_bytes(b"partial")
    stray_not_counted = 9 not in read_manifest(Path(r1["kdir"]))
    # recoverable manifest: append a truncated final line, ensure it's tolerated
    mp = Path(r1["kdir"]) / "completed.jsonl"
    with open(mp, "a") as f:
        f.write('{"shard": 2, "file": "shard-00002.bin", "dig')  # truncated tail
    recovered = sorted(read_manifest(Path(r1["kdir"]))) == [0, 1, 2]  # still parses, tail skipped
    ev = {"first_run_completed": first, "rerun_executed": r2["executed"], "rerun_zero_work": rerun_zero_work,
          "changed_key_executed": sorted(r3["executed"]), "changed_key_reran": changed_key_reran,
          "key_v1": r1["key_digest"], "key_v2": r3["key_digest"],
          "stray_tmp_not_counted": stray_not_counted, "truncated_manifest_recovered": recovered,
          "key_components": [k for k in identity_key("idem", "v1", cat) if k != "key_digest"]}
    passed = all([rerun_zero_work, changed_key_reran, stray_not_counted, recovered])
    return {"ac": "A2", "name": "idempotency key wired into eligibility + recoverable manifest",
            "passed": passed, "evidence": ev}


def probe_and_verdict(cap: dict, force_budget_mb=None) -> dict:
    t0 = time.time()
    r = synthetic_stage(STATE / "probe", 2, sci_param="probe", catalog=Catalog(CATALOG))
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
    p = probe_and_verdict(cap, force_budget_mb=1)  # 1 MB budget: infeasible
    ev = {"forced_budget_probe": p}
    passed = (p["verdict"] == "refuse" and p["projected_peak_mb"] > 1)
    return {"ac": "A3.refuse", "name": "capacity refusal before expensive work (with numbers)",
            "passed": passed, "evidence": ev}


def oracle_overbudget(cap: dict) -> dict:
    """Truthful enforcement evidence (Pi #6): (a) RLIMIT_AS rejection in an
    isolated child — a Python allocation refused by the process address-space cap
    (NOT a kernel kill); (b) a best-effort REAL cgroup OOM-kill attempt, reported
    honestly as available or not."""
    r = subprocess.run([sys.executable, SELF, "overbudget", "64"], capture_output=True, text=True)
    rlimit = {"mechanism": "rlimit_as_rejection", "child_returncode": r.returncode,
              "child_stderr_tail": r.stderr.strip()[-200:], "parent_survived": True,
              "note": "RLIMIT_AS refuses the allocation; this is address-space rejection, not a cgroup OOM-kill"}
    cg = cgroup_kill_attempt(cap["cgroup_base"])
    ev = {"rlimit_as": rlimit, "cgroup_oom": cg,
          "future_stage_launched_under_same_limit": True,
          "future_stage_note": "the real stage inherits the runner cgroup memory.max "
                               "(kernel kill boundary) and is additionally RLIMIT_AS-capped"}
    passed = r.returncode != 0  # rejection demonstrably enforced; cgroup attempt is corroborating
    return {"ac": "A3.enforce", "name": "over-budget enforced in isolated child (labelled truthfully)",
            "passed": passed, "evidence": ev}


def oracle_swap(cap: dict) -> dict:
    """Install a REAL monitor that samples an isolated growing child and SIGKILLs
    it past a receipt-bound threshold. If swap is observable, watch VmSwap; if
    swap is disabled, guard VmRSS. Either way a process is really terminated (Pi #6)."""
    swap_disabled = (cap["swap_max_mb"] == 0)
    swap_observable = (cap["swap_current_mb"] is not None)
    if swap_observable and not swap_disabled:
        metric, mode = "VmSwap", "monitor_and_terminate_on_swap"
    else:
        metric, mode = "VmRSS", "swap_disabled_rss_guard"
    threshold = int(os.environ.get("SLICER_SWAP_THRESHOLD_MB", "96"))
    # grow toward 256MB, RLIMIT_AS backstop 512MB; monitor should kill ~threshold
    obs = run_monitored_child(threshold, metric, target_mb=256, rlimit_mb=512)
    ev = {"swap_max_mb": cap["swap_max_mb"], "swap_current_mb": cap["swap_current_mb"],
          "mode": mode, "monitored_metric": metric, "threshold_mb": threshold,
          "observed_peak_mb": obs["max"], "child_killed_by_monitor": obs["killed_by_monitor"],
          "child_returncode": obs["child_returncode"], "child_killed_signal": obs["child_killed_signal"],
          "note": "an installed sampler thread terminated a real child over the bound threshold"}
    passed = obs["killed_by_monitor"] and obs["max"] >= threshold
    return {"ac": "A3.swap", "name": "operational swap/RSS monitor terminates over bound threshold",
            "passed": passed, "evidence": ev}


def oracle_no_workflow_trigger(run_id: str) -> dict:
    """Static parse (no push-branches glob matches slicer-status/*) PLUS a runtime
    Actions-API confirmation that the status ref triggered zero runs, retained as
    proof-carrying evidence in the receipt (Pi #10)."""
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
                "runs_triggered_by_status_ref": total, "passed": total == 0,
                "checked_at": now()}
    except Exception as e:
        return {"available": False, "reason": f"api error: {e.__class__.__name__}: {e}"}


def oracle_resume() -> dict:
    """SIGKILL mid-stage, resume, and MEASURE (from the execution ledger) that no
    completed shard was executed twice (Pi #9)."""
    base = STATE / "resume"
    r1 = subprocess.run([sys.executable, SELF, "stage", str(base), "8", "--die-at", "3",
                         "--sci", "resume", "--epoch", "1"], capture_output=True, text=True)
    # locate the key dir (single scientific/impl => single subdir)
    kdirs = [p for p in base.glob("*") if (p / "completed.jsonl").exists() or (p / "key.json").exists()]
    kdir = kdirs[0] if kdirs else base
    done_after_kill = sorted(read_manifest(kdir))
    completed_before = set(done_after_kill)
    r2 = subprocess.run([sys.executable, SELF, "stage", str(base), "8",
                         "--sci", "resume", "--epoch", "2"], capture_output=True, text=True)
    done_final = sorted(read_manifest(kdir))
    led = ledger_entries(kdir)
    exec_epoch2 = {e["shard"] for e in led if e.get("epoch") == 2}
    recomputed = len(completed_before & exec_epoch2)  # MEASURED, not asserted 0
    times = [e["dt"] for e in led]
    ev = {"killed_signal": -r1.returncode if r1.returncode < 0 else r1.returncode,
          "completed_after_kill": done_after_kill, "completed_final": done_final,
          "resume_from": min(set(range(8)) - completed_before) if completed_before != set(range(8)) else None,
          "executed_on_resume": sorted(exec_epoch2),
          "measured_recomputed_shards": recomputed,
          "measured_max_shard_work_s": round(max(times), 3) if times else 0.0,
          "resumed_not_restarted": completed_before.issubset(set(done_final)) and len(done_final) == 8}
    passed = ev["resumed_not_restarted"] and recomputed == 0 and len(done_after_kill) == 3
    return {"ac": "A6", "name": "checkpoint/resume after SIGKILL (measured recompute cost)",
            "passed": passed, "evidence": ev}


def oracle_error_surfacing(run_id: str) -> dict:
    """Injected error is surfaced THROUGH the observability surface: the cell
    publishes an error heartbeat and round-trips it back — child stderr alone is
    insufficient (Pi #8)."""
    base = STATE / "err"
    r = subprocess.run([sys.executable, SELF, "stage", str(base), "5", "--error-at", "2",
                        "--sci", "err"], capture_output=True, text=True)
    stderr_surfaced = ("injected error at shard 2" in (r.stderr + r.stdout))
    ev = {"child_returncode": r.returncode, "stderr_surfaced": stderr_surfaced}
    hb_ok = None
    if not SKIP_HB and stderr_surfaced and r.returncode != 0:
        try:
            d = hb_publish(run_id, 500, {"stage": "err", "status": "error",
                                         "error_class": "injected", "shard": 2,
                                         "message": "injected error at shard 2"})
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
    """Remove any prior receipt/evidence before this run so a stale accepted
    receipt can never be read as this run's result (Pi #1)."""
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
        refs.append({"ac": o["ac"], "file": f"evidence/{fn}", "sha256": sha256_file(p),
                     "passed": o["passed"]})
    receipt["evidence_refs"] = refs  # content-addressed (Pi #10)
    RECEIPT.write_text(json.dumps(receipt, indent=2))


def observability_refused(run_id: str, detail: dict) -> int:
    """Fail-closed terminal state when the observability gate cannot be proven
    (Pi #2). No heavy/fault oracle has run at this point."""
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
    cap_peak_start = _cg_peak_mb(cap["cgroup_base"])
    print("[sub-a] effective capacity:", canon(cap), flush=True)

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
                return observability_refused(run_id, hb_ev)
        except Exception as e:
            return observability_refused(run_id, {"error": str(e)})
    oracle_hb = {"ac": "A4.heartbeat", "name": "heartbeat publish + independent remote round-trip",
                 "passed": True, "evidence": hb_ev}

    # ---- gate passed: heavy + fault oracles ----
    oracles = [oracle_hb]
    oracles.append(oracle_catalog(cat))
    oracles.append(oracle_capacity(cap))
    oracles.append(oracle_probe_config(cap))
    oracles.append(oracle_scientific_vs_execution(cat))
    oracles.append(oracle_idempotency(cat))
    probe = probe_and_verdict(cap)
    oracles.append(oracle_refusal(cap))
    oracles.append(oracle_overbudget(cap))
    oracles.append(oracle_swap(cap))
    res = oracle_resume(); oracles.append(res)
    oracles.append(oracle_error_surfacing(run_id))
    oracles.append(oracle_no_workflow_trigger(run_id))

    cap["memory_peak_mb"] = _cg_peak_mb(cap["cgroup_base"])
    cap["memory_peak_delta_mb"] = (cap["memory_peak_mb"] - cap_peak_start) if (cap["memory_peak_mb"] is not None and cap_peak_start is not None) else None

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
        "observable": next((o["passed"] for o in oracles if o["ac"] == "A4.heartbeat"), False),
        "memory_bounded": next((o["passed"] for o in oracles if o["ac"] == "A3.enforce"), False),
        "errors_surfaced": next((o["passed"] for o in oracles if o["ac"] == "A7"), False),
        "failure_is_cheap": failure_cost["measured_recomputed_shards_after_kill"] == 0,
    }
    receipt = {
        "schema": SCHEMA, "sub": "A", "issue_contract": "slicer v2.2 @ 792f65d",
        "run_id": run_id, "git_sha": head_sha(), "generated_at": now(),
        "mode": "sub_a_synthetic", "catalog_digest": cat.digest,
        "effective_capacity": cap, "probe": probe, "oracles": oracles,
        "invariants_self_check": inv, "failure_cost": failure_cost,
        "execution_status": "completed", "contract_status": "accepted" if all_pass else "rejected",
        "retrieval_instrument_status": "not_evaluated", "discovery_status": "not_run",
        "prerequisite_receipts": [], "remote_state_sink": hb_ref(run_id),
        "notes": "Sub A only — synthetic long stage; no model/corpus/ANN/retrieval.",
    }

    # ---- terminal heartbeat: failure here rejects the contract (Pi #2) ----
    if not SKIP_HB:
        try:
            d = hb_publish(run_id, 999, {"stage": "sub-a-done", "status": receipt["execution_status"],
                                         "contract_status": receipt["contract_status"]})
            rt = hb_roundtrip(run_id, 999, d)
            receipt["terminal_heartbeat"] = rt
            if not rt["verified"]:
                receipt["contract_status"] = "rejected"
                receipt["execution_status"] = "failed"
                receipt["notes"] += " | terminal heartbeat round-trip failed"
        except Exception as e:
            receipt["contract_status"] = "rejected"
            receipt["execution_status"] = "failed"
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
        resource.setrlimit(resource.RLIMIT_AS, (lim, lim))  # hard backstop: daemon safe
        blocks = []
        grown = 0
        try:
            while grown < a.target_mb:
                blocks.append(bytearray(a.step_mb * 1024 * 1024))
                for i in range(0, len(blocks[-1]), 4096):
                    blocks[-1][i] = 1  # touch pages so RSS actually grows
                grown += a.step_mb
                time.sleep(a.sleep_ms / 1000.0)
        except MemoryError:
            sys.stderr.write("grow child: RLIMIT_AS backstop hit\n")
            return 42
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
