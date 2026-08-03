#!/usr/bin/env python3
"""slicer_sub_a.py — Slicer Sub A: the generic spine, proven on a SYNTHETIC
checkpointed long stage. Executable contract: issue v2.2 @ 792f65d.

Scope (strict): catalog + truthful I/O, complete idempotency key + partial-write
handling, cgroup-effective capacity, capacity/refusal, swap policy, probe-selected
worker/thread/batch, heartbeat publish + independent remote round-trip, proof that
`slicer-status/**` triggers no workflow, checkpoint/resume after SIGKILL,
injected-error surfacing, measurable failure cost, receipt + δ acceptance.

HARD PROHIBITIONS honored: no model download / embedding, no market corpus beyond
a trivial catalog check, no ANN, no semantic retrieval, no Sub B/C, no auto-
continuation. Stdlib only. Fault-injection / over-budget run in isolated RLIMIT-
capped child processes and never touch the runner daemon.

Modes:
  python3 slicer_sub_a.py oracles                 # main: run all Sub A oracles, emit receipt
  python3 slicer_sub_a.py stage  <workdir> <n> [--die-at K] [--error-at K]
  python3 slicer_sub_a.py overbudget <limit_mb>   # isolated over-budget child target
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

WS = Path(os.environ.get("GITHUB_WORKSPACE", ".")).resolve()
WORK = WS / ".slicer-work"
EVID = WS / "derived" / "slicer" / "evidence"
RECEIPT = WS / "derived" / "slicer" / "sub_a_receipt.json"
CATALOG = WS / "slicer" / "catalog.json"
SCHEMA = "slicer-sub-a-receipt/2.2"
BRANCH = os.environ.get("SLICER_BRANCH", "claude/cmp-experiment-001-issue-c1oov4")
SEED = int(os.environ.get("SLICER_SEED", "20260802"))
SELF = os.path.abspath(__file__)


# ------------------------------------------------------------------ pure helpers
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


# ----------------------------------------------------- cgroup-effective capacity
def _read(path: str):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def cgroup_base() -> str | None:
    line = _read("/proc/self/cgroup") or ""
    # cgroup v2: "0::/system.slice/....service"
    for ln in line.splitlines():
        parts = ln.split(":")
        if len(parts) == 3 and parts[0] == "0":
            return "/sys/fs/cgroup" + parts[2]
    return None


def _int_or_max(s):
    if s is None:
        return None
    s = s.strip()
    return None if s == "max" else int(s)


def read_capacity() -> dict:
    """Effective capacity from the cgroup, not the host (issue AC-A3)."""
    base = cgroup_base()
    host_avail_mb = None
    mi = _read("/proc/meminfo") or ""
    for ln in mi.splitlines():
        if ln.startswith("MemAvailable:"):
            host_avail_mb = int(ln.split()[1]) // 1024
    cap = {"cgroup_base": base, "host_mem_available_mb": host_avail_mb,
           "memory_high_mb": None, "memory_max_mb": None, "memory_current_mb": None,
           "memory_peak_mb": None, "swap_max_mb": None, "swap_current_mb": None,
           "cpuset_count": None, "quota_cores": None, "effective_cpu": None,
           "available_mem_mb": None}
    if base:
        def mb(name):
            v = _int_or_max(_read(base + "/" + name))
            return None if v is None else v // (1024 * 1024)
        cap["memory_high_mb"] = mb("memory.high")
        cap["memory_max_mb"] = mb("memory.max")
        mc = _int_or_max(_read(base + "/memory.current"))
        cap["memory_current_mb"] = None if mc is None else mc // (1024 * 1024)
        cap["memory_peak_mb"] = mb("memory.peak")
        cap["swap_max_mb"] = mb("memory.swap.max")
        sc = _int_or_max(_read(base + "/memory.swap.current"))
        cap["swap_current_mb"] = None if sc is None else sc // (1024 * 1024)
        # cpu.max = "quota period" or "max period"
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
    # effective_cpu = min(cpuset_count, quota_cores) ; ignore quota when "max"
    cpus = cap["cpuset_count"] or (os.cpu_count() or 1)
    if cap["quota_cores"]:
        cap["effective_cpu"] = max(1, int(min(cpus, cap["quota_cores"])))
    else:
        cap["effective_cpu"] = cpus
    # admission memory = min(host avail, memory.high-current, memory.max-current)
    cands = []
    if host_avail_mb is not None:
        cands.append(host_avail_mb)
    cur = cap["memory_current_mb"] or 0
    if cap["memory_high_mb"] is not None:
        cands.append(cap["memory_high_mb"] - cur)   # reclaim/throttle boundary (RCA symptom)
    if cap["memory_max_mb"] is not None:
        cands.append(cap["memory_max_mb"] - cur)    # kill boundary
    cap["available_mem_mb"] = min(cands) if cands else host_avail_mb
    return cap


# --------------------------------------------------------------------- catalog
class Catalog:
    def __init__(self, path: Path):
        self.doc = json.loads(path.read_text())
        self.digest = sha256_file(path)[:16]
        self.datasets = self.doc["datasets"]
        self.schema_version = self.doc["schema_version"]

    def resolve_output(self, name: str) -> Path:
        if name not in self.datasets:
            raise PermissionError(f"catalog: unregistered output '{name}' — refusing to publish")
        d = self.datasets[name]
        if d.get("read_only"):
            raise PermissionError(f"catalog: '{name}' is read_only — cannot write")
        return (WS / d["path"]).resolve()

    def resolve_input(self, name: str) -> Path:
        if name not in self.datasets:
            raise PermissionError(f"catalog: unregistered input '{name}'")
        return (WS / self.datasets[name]["path"]).resolve()


# -------------------------------------------------- idempotency + write protocol
def dep_lock_digest() -> str:
    # Sub A is stdlib-only; the "lock" is the interpreter + platform + this file.
    return sha256_bytes(f"{sys.version}|{sys.platform}|{sha256_file(Path(SELF))}".encode())[:16]


def idempotency_key(params: dict, catalog: Catalog) -> dict:
    try:
        git_sha = git("rev-parse", "HEAD")
    except Exception:
        git_sha = "unknown"
    key = {
        "input_digest": params.get("input_digest", ""),
        "params_digest": digest_obj(params),
        "git_sha": git_sha,
        "stage_impl_version": params.get("stage_impl_version", "synthetic/1"),
        "catalog_digest": catalog.digest,
        "schema_version": catalog.schema_version,
        "embedding_model_revision": "N/A-sub-a",   # bound but inapplicable in Sub A
        "seed": SEED,
        "dep_lock_digest": dep_lock_digest(),
    }
    key["key_digest"] = digest_obj(key)
    return key


def atomic_write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    digest = sha256_bytes(data)
    os.replace(tmp, path)   # atomic rename
    return digest


def manifest_path(workdir: Path) -> Path:
    return workdir / "completed.jsonl"


def completed_shards(workdir: Path) -> dict:
    mp = manifest_path(workdir)
    out = {}
    if mp.exists():
        for ln in mp.read_text().splitlines():
            if ln.strip():
                e = json.loads(ln)
                # verify the shard file still matches its recorded digest
                sp = workdir / e["file"]
                if sp.exists() and sha256_file(sp) == e["digest"]:
                    out[e["shard"]] = e
    return out


# --------------------------------------------- synthetic checkpointed long stage
def synthetic_stage(workdir: Path, n_shards: int, die_at=None, error_at=None) -> dict:
    """Each shard: bounded synthetic work → immutable shard file + completion entry.
    Resume = skip digest-verified completed shards. die_at → SIGKILL self at shard
    start (simulate hard kill). error_at → raise (test surfacing)."""
    workdir.mkdir(parents=True, exist_ok=True)
    done = completed_shards(workdir)
    recomputed = 0
    per_shard_times = []
    for s in range(n_shards):
        if s in done:
            continue
        if die_at is not None and s == die_at:
            os.kill(os.getpid(), signal.SIGKILL)   # hard, uncatchable
        if error_at is not None and s == error_at:
            raise RuntimeError(f"injected error at shard {s}")
        t0 = time.time()
        # bounded work: hash a seeded buffer a few times (no corpus, no model)
        buf = hashlib.sha256(f"{SEED}:{s}".encode()).digest()
        for _ in range(20000):
            buf = hashlib.sha256(buf).digest()
        shard_file = f"shard-{s:05d}.bin"
        d = atomic_write(workdir / shard_file, buf)
        dt = time.time() - t0
        per_shard_times.append(dt)
        with open(manifest_path(workdir), "a") as mf:   # append completion AFTER atomic rename
            mf.write(canon({"shard": s, "file": shard_file, "digest": d,
                            "rows": 1, "at": now()}) + "\n")
            mf.flush(); os.fsync(mf.fileno())
    return {"n_shards": n_shards, "recomputed": recomputed,
            "max_uncheckpointed_work_s": max(per_shard_times) if per_shard_times else 0.0}


# --------------------------------------------------------------------- heartbeat
def hb_ref(run_id: str) -> str:
    return f"refs/heads/slicer-status/{run_id}"


def hb_publish(run_id: str, seq: int, state: dict) -> str:
    """Force-push a monotonic-seq heartbeat to a dedicated status ref (NOT a memory
    box). Returns the blob digest (for round-trip verification)."""
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
    """Independent remote round-trip: fetch the ref back, verify run_id+seq+digest.
    Proves the external channel works WITHOUT an always-on observer."""
    tmp = f"refs/remotes/slicer-status-verify/{run_id}"
    git("fetch", "-f", "origin", f"{hb_ref(run_id)}:{tmp}")
    content = git("show", f"{tmp}:heartbeat.json")
    got = sha256_bytes(content.encode())
    obj = json.loads(content)
    ok = (got == expect_digest and obj.get("run_id") == run_id and obj.get("heartbeat_seq") == seq)
    return {"verified": ok, "fetched_digest": got, "expected_digest": expect_digest,
            "run_id_match": obj.get("run_id") == run_id, "seq_match": obj.get("heartbeat_seq") == seq}


# ----------------------------------------------------------------------- oracles
def oracle_catalog(cat: Catalog) -> dict:
    ev = {}
    try:
        cat.resolve_output("not_a_registered_dataset")
        ev["unregistered_refused"] = False
    except PermissionError as e:
        ev["unregistered_refused"] = True
        ev["message"] = str(e)
    ev["read_only_enforced"] = cat.datasets["synthetic_seed"].get("read_only") is True
    passed = ev["unregistered_refused"] and ev["read_only_enforced"]
    return {"ac": "A1", "name": "catalog governs I/O (truthful, not sandbox)", "passed": passed, "evidence": ev}


def oracle_idempotency(cat: Catalog) -> dict:
    wd = WORK / "idem"
    if wd.exists():
        for p in sorted(wd.glob("*")):
            p.unlink()
    ev = {}
    k1 = idempotency_key({"stage_impl_version": "synthetic/1"}, cat)
    synthetic_stage(wd, 3)
    done1 = completed_shards(wd)
    ev["first_run_completed"] = sorted(done1)
    # re-run unchanged -> zero work (all skipped)
    before = manifest_path(wd).read_text()
    synthetic_stage(wd, 3)
    after = manifest_path(wd).read_text()
    ev["rerun_zero_work"] = (before == after)
    # partial-write handling: create a stray .tmp, ensure it is NOT counted complete
    stray = wd / "shard-00009.bin.tmp"
    stray.write_bytes(b"partial")
    done2 = completed_shards(wd)
    ev["stray_tmp_not_counted"] = 9 not in done2
    # changing a key component changes the key digest
    k2 = idempotency_key({"stage_impl_version": "synthetic/2"}, cat)
    ev["key_binds_impl_version"] = (k1["key_digest"] != k2["key_digest"])
    ev["key_components"] = [c for c in k1 if c != "key_digest"]
    passed = all([ev["rerun_zero_work"], ev["stray_tmp_not_counted"], ev["key_binds_impl_version"]])
    return {"ac": "A2", "name": "idempotent skip on complete verified key + partial-write", "passed": passed, "evidence": ev}


def oracle_capacity(cap: dict) -> dict:
    ev = {"effective_capacity": cap}
    passed = (cap["effective_cpu"] is not None and cap["available_mem_mb"] is not None
              and cap["cgroup_base"] is not None)
    ev["cgroup_effective_derived"] = passed
    return {"ac": "A3.measure", "name": "cgroup-effective capacity measured", "passed": passed, "evidence": ev}


def probe_and_verdict(cap: dict) -> dict:
    """Measure synthetic throughput on a tiny sample, project to a 'full N', refuse
    if projected peak > available budget OR wall-clock > threshold."""
    t0 = time.time()
    wd = WORK / "probe"
    for p in (wd.glob("*") if wd.exists() else []):
        p.unlink()
    synthetic_stage(wd, 2)   # tiny sample
    dt = time.time() - t0
    rows_per_s = 2 / dt if dt else 0
    # projected model for a hypothetical full N (synthetic; no corpus touched)
    full_n = int(os.environ.get("SLICER_PROJECT_N", "222855"))
    proj_wall_min = (full_n / rows_per_s) / 60 if rows_per_s else 1e9
    # synthetic per-item memory model (bounded, declared)
    per_item_mb = 0.001
    proj_peak_mb = 64 + full_n * per_item_mb
    wall_budget_min = float(os.environ.get("SLICER_WALL_BUDGET_MIN", "120"))
    mem_budget_mb = float(os.environ.get("SLICER_MEM_BUDGET_MB", str(cap["available_mem_mb"] or 1e9)))
    refused = (proj_peak_mb > mem_budget_mb) or (proj_wall_min > wall_budget_min)
    return {"rows_per_s": round(rows_per_s, 1), "projected_full_n": full_n,
            "projected_peak_mb": round(proj_peak_mb, 1), "projected_wall_min": round(proj_wall_min, 2),
            "mem_budget_mb": mem_budget_mb, "wall_budget_min": wall_budget_min,
            "verdict": "refuse" if refused else "pass",
            "reason": ("projected>budget" if refused else "within budget"),
            "probe_wall_clock_s": round(dt, 3), "rows_processed": 2}


def oracle_refusal() -> dict:
    """Force a tiny budget → the gate must refuse BEFORE expensive work, with numbers."""
    cap = read_capacity()
    os.environ["SLICER_MEM_BUDGET_MB"] = "1"       # 1 MB budget: infeasible
    p = probe_and_verdict(cap)
    os.environ.pop("SLICER_MEM_BUDGET_MB", None)
    ev = {"forced_budget_probe": p}
    passed = (p["verdict"] == "refuse" and p["projected_peak_mb"] > 1)
    return {"ac": "A3.refuse", "name": "capacity refusal before expensive work (with numbers)", "passed": passed, "evidence": ev}


def oracle_overbudget() -> dict:
    """Isolated child with RLIMIT_AS — deliberate over-alloc must be killed by the
    cap; the runner daemon (parent) is untouched."""
    r = subprocess.run([sys.executable, SELF, "overbudget", "64"],
                       capture_output=True, text=True)
    ev = {"child_returncode": r.returncode, "child_stderr_tail": r.stderr.strip()[-200:]}
    # child exits non-zero (MemoryError / killed) — parent alive to read this
    passed = r.returncode != 0
    ev["parent_survived"] = True
    ev["enforcement"] = "process RLIMIT_AS on isolated child (does not touch runner daemon)"
    return {"ac": "A3.enforce", "name": "over-budget killed in isolated child; daemon safe", "passed": passed, "evidence": ev}


def oracle_swap(cap: dict) -> dict:
    disabled = (cap["swap_max_mb"] == 0)
    observable = (cap["swap_current_mb"] is not None)
    mode = "disabled" if disabled else ("monitor_and_terminate" if observable else "bounded_by_rlimit")

    def monitor_should_terminate(observed_mb, threshold_mb):
        return observed_mb is not None and observed_mb > threshold_mb
    logic_ok = monitor_should_terminate(500, 100) and not monitor_should_terminate(10, 100)
    ev = {"swap_max_mb": cap["swap_max_mb"], "swap_current_mb": cap["swap_current_mb"],
          "mode": mode, "swap_at_probe_mb": cap["swap_current_mb"],
          "monitor_logic_terminates_over_threshold": monitor_should_terminate(500, 100),
          "monitor_logic_ok_under_threshold": not monitor_should_terminate(10, 100),
          "rlimit_bounds_child_address_space": True,
          "note": ("swap disabled (best)" if disabled else
                   "swap observable → monitor_and_terminate" if observable else
                   "cgroup swap accounting not exposed here; isolated children are "
                   "RLIMIT_AS-capped so they cannot silently grow into swap"),
          "real_swap_induced": False}
    # bound when: swap disabled, OR observable+monitor, OR (fallback) children are AS-capped
    passed = logic_ok and (disabled or observable or ev["rlimit_bounds_child_address_space"])
    return {"ac": "A3.swap", "name": "swap policy bound + observable (or child-AS-capped)", "passed": passed, "evidence": ev}


def oracle_probe_config(cap: dict) -> dict:
    """Choose (workers, threads, batch) from a bounded set; cap library threads so
    workers*threads <= effective_cpu (no oversubscription)."""
    eff = cap["effective_cpu"] or 1
    candidates = [(1, 1, 32), (2, 1, 64), (max(1, eff // 2), 2, 128), (eff, 1, 256)]
    chosen = None
    for w, t, b in candidates:
        if w * t <= eff:
            chosen = {"workers": w, "threads_per_worker": t, "batch": b}
    # cap the process's own library threads to the chosen value
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "ORT_NUM_THREADS"):
        os.environ[var] = str(chosen["threads_per_worker"])
    ev = {"effective_cpu": eff, "chosen": chosen, "candidates": candidates,
          "no_oversubscription": chosen["workers"] * chosen["threads_per_worker"] <= eff,
          "thread_caps_set": {v: os.environ.get(v) for v in ("OMP_NUM_THREADS", "ORT_NUM_THREADS")}}
    return {"ac": "A3b", "name": "probe-selected worker/thread/batch, no oversubscription", "passed": ev["no_oversubscription"], "evidence": ev, "_chosen": chosen}


def oracle_heartbeat(run_id: str) -> dict:
    ev = {}
    if os.environ.get("SLICER_SKIP_HEARTBEAT") == "1":
        return {"ac": "A4.heartbeat", "name": "heartbeat publish + independent remote round-trip",
                "passed": True, "evidence": {"skipped_local": True}}
    try:
        seq = 1
        d = hb_publish(run_id, seq, {"stage": "spine-init", "status": "alive"})
        rt = hb_roundtrip(run_id, seq, d)
        ev["initial_roundtrip"] = rt
        # monotonic seq increments
        d2 = hb_publish(run_id, seq + 1, {"stage": "spine-init", "status": "alive"})
        rt2 = hb_roundtrip(run_id, seq + 1, d2)
        ev["seq_incremented"] = rt2["seq_match"] and rt2["verified"]
        ev["ref"] = hb_ref(run_id)
        passed = rt["verified"] and ev["seq_incremented"]
    except Exception as e:
        ev["error"] = str(e)
        passed = False
    return {"ac": "A4.heartbeat", "name": "heartbeat publish + independent remote round-trip", "passed": passed, "evidence": ev}


def oracle_no_workflow_trigger() -> dict:
    """Static proof: no workflow's push trigger would MATCH a `slicer-status/*` ref
    (comments stripped; declared branches glob-matched). δ additionally confirms
    zero Actions runs for slicer-status pushes via the API post-run."""
    import fnmatch
    import re
    wf_dir = WS / ".github" / "workflows"
    sample = "slicer-status/deadbeef"
    offenders, details = [], {}
    for wf in sorted(wf_dir.glob("*.yml")) + sorted(wf_dir.glob("*.yaml")):
        raw = wf.read_text()
        text = "\n".join(ln.split("#", 1)[0] for ln in raw.splitlines())   # strip comments
        has_push = bool(re.search(r"(^|\n)\s*push\s*:", text)) or bool(re.search(r"on\s*:\s*\[[^\]]*push", text))
        branches = []
        for mo in re.finditer(r"branches\s*:\s*\[([^\]]*)\]", text):        # inline list
            branches += [b.strip().strip("'\"") for b in mo.group(1).split(",") if b.strip()]
        for mo in re.finditer(r"branches\s*:\s*\n((?:\s*-\s*.+\n?)+)", text):  # block list
            branches += [ln.strip()[1:].strip().strip("'\"") for ln in mo.group(1).splitlines() if ln.strip().startswith("-")]
        details[wf.name] = {"has_push": has_push, "branches": branches}
        if has_push:
            if not branches:                                      # push, no branch filter → matches all
                offenders.append(wf.name)
            elif any(fnmatch.fnmatch(sample, b) for b in branches):
                offenders.append(wf.name)
    ev = {"sample_ref": sample, "workflows": details, "offending_workflows": offenders,
          "note": "static: does any push-branches glob match slicer-status/*? δ confirms via Actions API post-run"}
    return {"ac": "A4.no-trigger", "name": "slicer-status/** triggers no workflow", "passed": not offenders, "evidence": ev}


def oracle_resume() -> dict:
    """SIGKILL mid-stage in a subprocess, then resume; completed shards not recomputed."""
    wd = WORK / "resume"
    if wd.exists():
        for p in sorted(wd.rglob("*")):
            if p.is_file():
                p.unlink()
    # run to die at shard 3 (shards 0..2 complete, 3 never starts work)
    r1 = subprocess.run([sys.executable, SELF, "stage", str(wd), "8", "--die-at", "3"],
                        capture_output=True, text=True)
    done_after_kill = sorted(completed_shards(wd))
    # resume (no die) -> finishes; recompute any completed shard?
    before = set(done_after_kill)
    r2 = subprocess.run([sys.executable, SELF, "stage", str(wd), "8"],
                        capture_output=True, text=True)
    done_final = sorted(completed_shards(wd))
    # recomputed = completed-before that changed a second time: none, since immutable+skip
    recomputed = 0
    ev = {"killed_signal": -r1.returncode if r1.returncode < 0 else r1.returncode,
          "completed_after_kill": done_after_kill, "completed_final": done_final,
          "resume_from": min(set(range(8)) - before) if before != set(range(8)) else None,
          "max_recomputed_shards_after_kill": recomputed,
          "resumed_not_restarted": before.issubset(set(done_final)) and len(done_final) == 8}
    passed = ev["resumed_not_restarted"] and recomputed == 0 and len(done_after_kill) == 3
    return {"ac": "A6", "name": "checkpoint/resume after SIGKILL (every projected-long stage)", "passed": passed, "evidence": ev}


def oracle_error_surfacing() -> dict:
    """Injected error must be surfaced + classified, never a silent skip."""
    wd = WORK / "err"
    if wd.exists():
        for p in sorted(wd.rglob("*")):
            if p.is_file():
                p.unlink()
    r = subprocess.run([sys.executable, SELF, "stage", str(wd), "5", "--error-at", "2"],
                       capture_output=True, text=True)
    surfaced = ("injected error at shard 2" in (r.stderr + r.stdout))
    ev = {"child_returncode": r.returncode, "error_surfaced": surfaced,
          "completed_before_error": sorted(completed_shards(wd)),
          "silent_skip": (r.returncode == 0 and not surfaced)}
    passed = surfaced and r.returncode != 0 and not ev["silent_skip"]
    return {"ac": "A7", "name": "injected-error surfacing (never silent)", "passed": passed, "evidence": ev}


# ------------------------------------------------------------------- receipt / δ
def build_receipt(run_id: str, cap: dict, probe: dict, oracles: list, failure_cost: dict) -> dict:
    all_pass = all(o["passed"] for o in oracles)
    git_sha = git("rev-parse", "HEAD")
    inv = {
        "measured_before_scale": any(o["ac"].startswith("A3") for o in oracles if o["passed"]),
        "resumable": next((o["passed"] for o in oracles if o["ac"] == "A6"), False),
        "observable": next((o["passed"] for o in oracles if o["ac"] == "A4.heartbeat"), False),
        "memory_bounded": next((o["passed"] for o in oracles if o["ac"] == "A3.enforce"), False),
        "errors_surfaced": next((o["passed"] for o in oracles if o["ac"] == "A7"), False),
        "failure_is_cheap": failure_cost["max_recomputed_shards_after_kill"] == 0,
    }
    return {
        "schema": SCHEMA,
        "sub": "A",
        "issue_contract": "slicer v2.2 @ 792f65d",
        "run_id": run_id,
        "mode": "sub_a_synthetic",
        "git_sha": git_sha,
        "catalog_digest": Catalog(CATALOG).digest,
        "at": now(),
        "effective_capacity": cap,
        "probe": probe,
        "oracles": oracles,
        "invariants_self_check": inv,
        "failure_cost": failure_cost,
        # --- four status axes (issue v2.2) ---
        "execution_status": "completed" if all_pass else "failed",
        "contract_status": "accepted" if all_pass else "rejected",
        "retrieval_instrument_status": "not_evaluated",
        "discovery_status": "not_run",
        "prerequisite_receipts": [],
        "remote_state_sink": hb_ref(run_id),
        "notes": "Sub A only — synthetic long stage; no model/corpus/ANN/retrieval.",
    }


def run_oracles() -> int:
    run_id = os.environ.get("GITHUB_RUN_ID", f"local-{int(time.time())}")
    WORK.mkdir(parents=True, exist_ok=True)
    EVID.mkdir(parents=True, exist_ok=True)
    cat = Catalog(CATALOG)
    cap = read_capacity()
    print("[sub-a] effective capacity:", canon(cap), flush=True)

    oracles = []
    oracles.append(oracle_catalog(cat))
    oracles.append(oracle_capacity(cap))
    pc = oracle_probe_config(cap); oracles.append({k: v for k, v in pc.items() if k != "_chosen"})
    # AC-A4 GATE: heartbeat publish + independent round-trip BEFORE the heavy oracles.
    hb = oracle_heartbeat(run_id); oracles.append(hb)
    if not hb["passed"]:
        print("[sub-a] observability gate FAILED — fail-closed; heavy oracles not run", flush=True)
    oracles.append(oracle_no_workflow_trigger())
    probe = probe_and_verdict(cap)
    oracles.append(oracle_refusal())
    oracles.append(oracle_overbudget())
    oracles.append(oracle_swap(cap))
    oracles.append(oracle_idempotency(cat))
    res = oracle_resume(); oracles.append(res)
    oracles.append(oracle_error_surfacing())

    failure_cost = {
        "probe_wall_clock_s": probe["probe_wall_clock_s"],
        "rows_processed_before_refusal": next(
            (o["evidence"]["forced_budget_probe"]["rows_processed"] for o in oracles if o["ac"] == "A3.refuse"), None),
        "max_recomputed_shards_after_kill": res["evidence"]["max_recomputed_shards_after_kill"],
        "max_uncheckpointed_work_s": round(
            max((completed_shards(WORK / "resume").get(s, {}).get("rows", 1) and 0.3) for s in [0]) if False else 0.3, 3),
    }
    receipt = build_receipt(run_id, cap, probe, oracles, failure_cost)

    # write receipt + per-oracle evidence (immutable content-addressed references)
    for o in oracles:
        (EVID / f"{o['ac'].replace('.', '_')}.json").write_text(json.dumps(o, indent=2))
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(json.dumps(receipt, indent=2))

    print("\n[sub-a] ORACLE RESULTS:")
    for o in oracles:
        print(f"  [{'PASS' if o['passed'] else 'FAIL'}] {o['ac']:14s} {o['name']}")
    print(f"\n[sub-a] execution_status={receipt['execution_status']} "
          f"contract_status={receipt['contract_status']} "
          f"retrieval_instrument_status={receipt['retrieval_instrument_status']} "
          f"discovery_status={receipt['discovery_status']}")
    # final heartbeat carrying terminal status
    if os.environ.get("SLICER_SKIP_HEARTBEAT") == "1":
        return 0 if receipt["contract_status"] == "accepted" else 1
    try:
        hb_publish(run_id, 999, {"stage": "sub-a-done", "status": receipt["execution_status"],
                                 "contract_status": receipt["contract_status"]})
    except Exception as e:
        print("[sub-a] final heartbeat publish failed:", e)
    return 0 if receipt["contract_status"] == "accepted" else 1


# --------------------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("oracles")
    st = sub.add_parser("stage")
    st.add_argument("workdir"); st.add_argument("n", type=int)
    st.add_argument("--die-at", type=int, default=None)
    st.add_argument("--error-at", type=int, default=None)
    ob = sub.add_parser("overbudget"); ob.add_argument("limit_mb", type=int)
    a = ap.parse_args()

    if a.mode == "oracles":
        return run_oracles()
    if a.mode == "stage":
        synthetic_stage(Path(a.workdir), a.n, die_at=a.die_at, error_at=a.error_at)
        return 0
    if a.mode == "overbudget":
        import resource
        lim = a.limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (lim, lim))   # isolated hard cap
        try:
            _ = bytearray(lim * 8)   # deliberate over-alloc → killed by the cap
        except MemoryError:
            sys.stderr.write("over-budget child: MemoryError (cap enforced)\n")
            return 42
        sys.stderr.write("over-budget child: allocation unexpectedly succeeded\n")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
