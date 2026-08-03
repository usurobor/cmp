# Box Ref-Runner (`celld`) — a git-ref-driven cell executor

Status: **draft for review** (Pi / Axiom)
Author: δ (cloud Sigma)
Supersedes: the *dispatch mechanism* of `docs/design/compute-pipeline-architecture.md`
(GitHub Actions self-hosted runner as the cell trigger). The runner-as-cell
*topology* — δ dispatches, the cell is the run, the box compacts — is unchanged;
only the trigger/transport changes from a GitHub Actions workflow to a box-side
poller reading a git ref.

Applies cnos L7 `design` (invariants, information hiding, dependency direction,
truthful interfaces) and `eng/performance-reliability` (workload → budgets → cost
→ amplification → failure → degradation → recovery → operator-visible → gaps),
`eng/write-functional`, `eng/process-economics`.

---

## 1. Motivation

The GitHub Actions path works but couples the cell to a mechanism we do not
control and that has proven unreliable to depend on. We want a runner that:

- executes jobs on `cn-sigma` with **no GitHub Action in the loop**;
- is driven by a git ref **δ writes** and posts **progress + completion** to git
  refs anyone with read can verify (L3: observable-without-privilege);
- keeps the **capacity/observability pre-flight gate** that the embed-blowup RCA
  (`threads/rca/2026-08-01-embed-memory-blowup.md`) said was missing;
- runs fault-injection / over-budget work in isolated cgroup/RLIMIT children that
  **cannot endanger the poller daemon** — cleaner than protecting a third-party
  runner daemon.

Non-goal: a general CI system. This runs a small, declared set of **cell kinds**
for cmp (Slicer today), nothing else.

---

## 2. Invariants (what must stay true)

| # | Invariant | Why |
|---|-----------|-----|
| I1 | **The ref is a request; the box's local policy is the authority.** The box runs only allowlisted `kind`s at a pinned `commit_sha` via a fixed entrypoint template — never an arbitrary command from the ref. | The dispatch ref is an RCE surface. Mirrors ω's boundary: act on box-scoped requests within declared policy, never arbitrary relayed authority. |
| I2 | **At-most-once execution per `job_id`.** | Duplicate runs waste the box and can double-write receipts. |
| I3 | **Every projected-long cell is resumable** from durable checkpoint state (L2) outside the checkout. | The RCA was lost work across aborted runs. |
| I4 | **Silence is falsifiable** (L3): a runner-liveness heartbeat independent of any job, monotonic per-job status seq, and a job **lease** with an expiry. Dead never looks like done. | Observability without an always-on privileged observer. |
| I5 | **Capacity + observability pre-flight gate** before any heavy work; refuse (do not launch) when over budget (L1, L4). | The direct RCA fix. |
| I6 | **Fault/over-budget work is isolated** in cgroup+RLIMIT children; the poller daemon is never at risk (L5/L6). | Failure must be cheap, not fatal. |
| I7 | **No ref is ever deleted.** State transitions are append/overwrite only (tombstone states, not `git push --delete`). | The git proxy blocks ref deletion; design around it. |
| I8 | **Receipts are proof-carrying and fresh** (`job_id`, `commit_sha`, per-evidence sha256), same discipline as the Slicer receipt. | Stale-result false-greens must be unreachable. |

---

## 3. Topology & trust

```
 δ (cloud Sigma)                 cn-sigma box                     any reader
 ───────────────                 ─────────────                    ──────────
 writes job spec  ──push──▶  refs/cells/dispatch
                                    │  (celld polls, verifies vs local policy)
                                    ▼
                             celld: claim → gate → run cell (cgroup/RLIMIT)
                                    │
        progress/heartbeat ◀─push── ├─▶ refs/cells/status/<job_id>   ──fetch──▶ δ/Pi
        receipt+evidence   ◀─push── ├─▶ refs/cells/receipts/<job_id> ──fetch──▶ δ/Pi
        liveness           ◀─push── └─▶ refs/cells/runnerd           ──fetch──▶ δ/Pi
```

- **Writer of `refs/cells/dispatch`:** δ only (push credential). A spec is a
  *request*, authorized only if it matches box-local policy (§6).
- **Writer of `refs/cells/status/*`, `refs/cells/receipts/*`, `refs/cells/runnerd`:**
  the box (`celld`) only. Single writer per ref (cnos#690).
- **`celld`** runs as a dedicated unprivileged systemd service, single instance,
  under a cgroup it can sub-delegate to children.

---

## 4. The ref protocol (truthful interfaces)

Refs carry small JSON blobs (git trees of one/few files), force-pushed for
single-value refs, fast-forward-appended for the dispatch thread.

### 4.1 Dispatch — `refs/cells/dispatch` (writer: δ, append-only thread, #690 shape)
Tree holds `jobs/<job_id>.json`. A job spec:

```json
{
  "job_id": "slicer-sub-a-20260803T150000Z",
  "kind": "slicer.oracles",
  "repo": "usurobor/cmp",
  "commit_sha": "5045509…",
  "budgets": { "wall_s": 7200, "mem_mb": 10000 },
  "env_allow": { "SLICER_STATE_DIR": "/home/sigma/.cmp/slicer/state" },
  "created_at": "2026-08-03T15:00:00Z",
  "created_by": "delta",
  "sig": "ssh-ed25519 …"            // optional; see §6
}
```

The spec names **no command**. The command comes from box policy for `kind`
(§6). `commit_sha` is the pinned executable contract.

### 4.2 Status — `refs/cells/status/<job_id>` (writer: box, force-push, monotonic seq)
```json
{ "job_id":"…", "seq": 7, "at":"…",
  "state":"queued|claimed|admitted|refused|running|completed|failed|dead_letter",
  "lease_expires_at":"…", "phase":"probe|stage:shard=3|…",
  "detail": { … } }
```

### 4.3 Receipt — `refs/cells/receipts/<job_id>` (writer: box)
The cell's own proof-carrying receipt (e.g. the Slicer Sub A receipt) plus a
runner envelope: `{ job_id, commit_sha, kind, started_at, ended_at,
evidence_refs:[{file,sha256}], exit_state }`.

### 4.4 Runner liveness — `refs/cells/runnerd` (writer: box, force-push, monotonic seq)
Heartbeat proving `celld` is alive **even when idle** (I4): `{ seq, at,
current_job|null, capacity_snapshot }`. A reader that sees `runnerd` stale AND a
job status stale-with-expired-lease knows the box is down — not that the job is
done.

---

## 5. Poller loop & job lifecycle

State machine (each transition posted to `status/<job_id>`):

```
queued ──claim(lease)──▶ claimed ──preflight──▶ admitted ──▶ running ──▶ completed
                                        └─(over budget / bad spec)─▶ refused
   running ──crash/lease-expiry──▶ (reclaim) ──resume from checkpoint──▶ running
   running ──error──▶ failed ──(attempts<N)──▶ queued ; ──(attempts≥N)──▶ dead_letter
```

`celld` loop (functional core is pure decisions; effects at the edges):

1. **fetch** `refs/cells/dispatch`; parse specs (pure). Ignore malformed specs
   (I7 fail-safe: never execute a spec you cannot fully verify).
2. **select** the oldest job with no terminal status and no live lease (pure).
3. **claim**: write `status:{state:claimed, lease_expires_at}`, **round-trip
   verify** (fetch it back) before doing work (at-most-once, I2).
4. **authorize vs policy** (§6): kind allowlisted? entrypoint resolved? budgets
   within caps? sha present? Else `state:refused`.
5. **pre-flight gate** (I5): measure cgroup-effective capacity; run the cell's
   own probe/admission; if projected > budget or no checkpoint boundary for a
   long stage → `state:refused` (do not launch).
6. **checkout** `repo@commit_sha` into a clean per-job workdir (state/checkpoints
   live outside it, I3).
7. **run** the policy entrypoint in an isolated child under a delegated cgroup
   (`memory.max`, `memory.swap.max`) + `RLIMIT_AS` + `wall_s` timeout; renew the
   lease and push `status` on each heartbeat tick; the cell streams its own
   phase/progress.
8. **publish** receipt+evidence to `refs/cells/receipts/<job_id>`; final
   `status:{completed|failed}`.
9. **retry/dead-letter** on failure per §8.

Concurrency: **1 job at a time** initially (box is 8 vCPU / 16 GB; a cell already
uses the box). A small fixed pool is a later option, gated by capacity.

---

## 6. Authorization & execution (the RCE surface, hardened)

**Box-local policy** (`/etc/celld/policy.json`, owned by the box, never sourced
from the ref) declares the only things `celld` will run:

```json
{
  "kinds": {
    "slicer.oracles": {
      "repo": "usurobor/cmp",
      "entrypoint": ["python3","scripts/slicer_sub_a.py","oracles"],
      "budget_caps": { "wall_s": 10800, "mem_mb": 12000 },
      "env_allow": ["SLICER_STATE_DIR","SLICER_SEED","OMP_NUM_THREADS","ORT_NUM_THREADS"]
    }
  },
  "require_signed_dispatch": true,
  "authorized_signers": ["ssh-ed25519 …delta-key…"]
}
```

Rules (I1):
- The ref may only **select** a kind and **narrow** budgets within `budget_caps`;
  it can never introduce a command, path, or env var not in policy.
- `commit_sha` must exist in `repo`; the entrypoint script is run **as committed
  at that sha**, nothing else. No shell, no `eval`, no spec-supplied argv.
- If `require_signed_dispatch`, the spec must carry a valid signature from an
  `authorized_signers` key over its canonical bytes — so a compromised
  dispatch-ref write alone cannot inject a job. (At minimum: sha pin + allowlist.)
- Runs as an unprivileged user, in a per-job cgroup, with only `env_allow` vars.

This is the same shape as ω's authority boundary: **box-scoped, specific, policy-
bounded requests are honored; arbitrary or outward-facing ones are not.**

---

## 7. Isolation & capacity (L1/L4/L5/L6)

- **cgroup:** `celld` creates a per-job sub-cgroup, sets `memory.max` (kill
  boundary) and `memory.swap.max`, and reads `memory.peak` for a true per-job
  peak (the delegated cgroup the Slicer receipt flagged as needed for Sub B).
- **RLIMIT_AS** belt-and-suspenders on the child; **wall_s** timeout kills a hung
  job; both leave the daemon alive.
- **Capacity measured before scale** (cgroup-effective cpu/mem, safety factor) —
  reused from `scripts/slicer_sub_a.py:read_capacity`.

---

## 8. Performance & reliability

**Workload.** One cell dispatch → poll → claim → gate → run → receipt cycle.

**Budgets (named).** `poll_interval` 15 s · `heartbeat_interval` 20 s ·
`lease_ttl` 90 s (> a few missed heartbeats) · `max_job_wall_s` (per-kind cap) ·
`retry_max` 2 · `dead_letter` after `retry_max` · `max_concurrent_jobs` 1.

**Steady-state cost.** Idle: one fetch + one `runnerd` push per `poll_interval`.
Active: one child process, N status pushes = ⌈wall/heartbeat_interval⌉, one
receipt push. All bounded.

**Amplification paths.** (a) A crash-looping job → bounded by `retry_max` then
dead-letter. (b) Status push fan-out → one per heartbeat tick, capped by
`max_job_wall_s/heartbeat_interval`. (c) Dispatch thread growth → δ appends;
compaction is the box's job later (#690), not `celld`'s.

**Failure modes & degradation.**
| Failure | Behavior |
|---------|----------|
| `celld` crash | systemd restarts; in-flight job's lease expires; on restart it reclaims and **resumes from checkpoint** (I3). |
| Two executors (shouldn't happen) | single systemd instance + claim round-trip; loser sees live lease and skips. |
| Job hangs | `max_job_wall_s` timeout kills the isolated child; `state:failed`. |
| Box OOM pressure | per-job `memory.max` kills the child, not the daemon; capacity gate refuses over-budget jobs up front. |
| Can't push status (network) | durable local status log; retry with backoff; on reconnect, publish. Readers see stale seq + expired lease → **known dead, not done** (I4). |
| Malformed / unauthorized spec | `state:refused` (or ignored if unparseable); never executed (I1/I7). |
| Poison job | `retry_max` then `state:dead_letter` with reason; loop continues. |
| Ref delete blocked | never used; terminal states are tombstones (I7). |

**Recovery.** Durable checkpoints (resume), lease reclaim after expiry,
idempotent execution keyed by `job_id`+identity-key, dead-letter as the terminal
sink.

**Operator-visible truth.** `refs/cells/runnerd` (box liveness + capacity),
`refs/cells/status/<job_id>` (phase/state/lease), `refs/cells/receipts/<job_id>`
(proof), and a `celld doctor` that prints current job, lease, last heartbeat age,
and refusal reasons.

**Known gaps.** (1) Signature verification (`require_signed_dispatch`) is the
hardening line; until keys are provisioned we rely on push-auth + sha-pin +
allowlist. (2) Single-box, single-instance; no HA. (3) Dispatch-thread compaction
is deferred to the box (#690) — unbounded growth is slow but real; needs a sunset
policy. (4) Cross-box scheduling is out of scope.

---

## 9. Acceptance criteria (file + check + pass/fail, cdd-style)

- **AC-R1 at-most-once:** kill `celld` mid-job, restart; check `receipts/<job_id>`
  has exactly one terminal receipt and the exec-ledger shows **0 recomputed**
  completed shards. Pass iff both.
- **AC-R2 refusal:** dispatch a job whose probe projects over budget; check
  `status` reaches `refused` and **no** child cell process ran. Pass iff refused
  before launch.
- **AC-R3 isolation:** dispatch an over-budget cell; check the child is
  cgroup/RLIMIT-killed and `celld` (pid) survives. Pass iff daemon alive.
- **AC-R4 liveness-falsifiable:** stop `celld`; check `runnerd` heartbeat goes
  stale and the in-flight `status` lease expires within `lease_ttl`. Pass iff a
  reader can distinguish dead from done.
- **AC-R5 policy authority:** dispatch a spec with a non-allowlisted `kind` or an
  entrypoint not in policy; check it is `refused`/ignored and never executed.
  Pass iff no execution.
- **AC-R6 pinned sha:** dispatch `kind` at a `commit_sha`; check the entrypoint
  ran from exactly that tree (receipt `commit_sha` matches). Pass iff match.
- **AC-R7 no-delete:** run the full lifecycle; check no ref deletion was
  attempted and terminal states are tombstones. Pass iff no delete calls.

---

## 10. Migration

1. Land `celld` + policy on `cn-sigma` alongside the existing workflow (no
   cutover). Prove AC-R1..R7 with the current Slicer Sub A as `kind:
   slicer.oracles`.
2. Switch Slicer dispatch to `refs/cells/dispatch`; δ writes specs instead of
   pushing source to trigger the Action.
3. Retire `.github/workflows/slicer-sub-a.yml` once the ref path is accepted.
4. Reuse the same runner for Sub B/C as new `kind`s (policy additions), no new
   transport.

---

## 11. Open questions for Pi / Axiom

- Signature requirement from day one, or sha-pin + allowlist first and signing
  as a fast-follow?
- `celld` implementation language: stdlib Python (matches Slicer, fastest path)
  vs a compiled daemon. Recommendation: stdlib Python first; it is the lightest
  thing that earns its cost.
- Dispatch-thread compaction/sunset policy owner (box, per #690) — when?
