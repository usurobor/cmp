# Box job admission — design

**Status:** `v0.4.0` — **design only, nothing built.** Architecture converged with
Pi; this version closes the seven remaining executable-contract corrections Pi
raised on v0.3.0 so the design is implementation-dispatchable without another
architectural pass.
**Design:** ω (Omega). **Converged:** δ (cn-sigma@cmp:claude/chat).
**Applies:** cnos.eng `evolve`, `code`, `process-economics`, core `design`,
`performance-reliability`, L7.

**v0.4.0 changelog (Pi's 7):** writer identity + ref-protection for **all three**
operational refs, not just the queue (§3.1); `jobs/runner` is a **mutable
single-writer snapshot**, not a 525k-commits/year append log (§3.1, §4.5); frozen
**wire format, canonicalization, cursor law, and status-event schema** (§3.6); a
**single** job-id grammar with a bounded length (§3.2); a **hardened root
launcher** — independent re-resolution, digest/schema re-validation, TOCTOU/
symlink defenses, fd/spool-only input, explicit invocation authority,
`WorkingDirectory` (§4.2); timeout that **refuses over-cap projections** and a
defined, bound projection source (§4.3); an **atomic exactly-once ledger
contract** with reconciliation on both sides of the launch boundary (§3.5); and
acceptance criteria that prove the new trust claims (§6).

---

## Authority

Owns **how work arrives at the box and what admits it**. Does **not** own what the
work is (that is Slicer; Slicer supersedes on anything executable inside a job).
Deliberately narrow — not a workflow engine, not a second stage runner.

---

## 1. Problem

**The recurring class: ad-hoc invocation with no admission contract.** Each job
reaches the box by a bespoke path with bespoke (or absent) limits, identity,
status, replay — so each fails a new way. Evidence, 2026-08-01→03, one box:

| # | failure | why *invocation* caused it |
|---|---|---|
| 1 | 21 abandoned sessions exhausted RAM; SSH "hung" | nothing bounded process lifetime |
| 2 | 1.7 GB job → **global** OOM killed the runner, twice | no per-job cgroup |
| 3 | processes survived cancelled runs | lifetime not bound to a supervised unit |
| 4 | encode wrote into the CI workspace, near `git clean -ffdx` | output path was convention |
| 5 | encode ran 2h20m at 3.2% unobserved | progress per-script, absent by default |
| 6 | 5× "fetch failure ≈ no data" | status hand-rolled per consumer |
| 7 | every run unresumable | replay per-script |

Slicer fixes 4–7 *for Slicer*; it does not fix 1–3, and nothing stops the next
script repeating 4–7 — those are properties of **how a job is invoked**.

**The trust problem.** The invocation path was a self-hosted GitHub Actions runner
— GitHub decides what executes on the box. While `cmp` was public: fork → PR
approved once → later PRs auto-run as `sigma` (NOPASSWD sudo ALL) → root on the
corpus box. Re-privatising mitigated it, but the *shape* survives: an inbound
execution channel whose safety rests on repo visibility and approval discipline,
not structure. (Visibility has since flipped again for the comms bridge — proof
"safe because private" is not structural.)

---

## 2. The boundary move (L7)

**Invert control of invocation:** the box **pulls** work it decides to accept
instead of GitHub pushing work onto it. Structurally: no inbound *code* channel;
status refs stop being dangerous; limits/identity/lifetime become one uniform
container; Slicer becomes invocation-portable; the self-hosted runner can be
unregistered.

**Narrowed security claim (Pi).** Not "nothing outside can cause the box to run
anything." The warranted claim: **no outside party can supply code, command,
executable path, or resource policy; an outside actor may only *request* a known
registered job, and the box's local policy independently admits or rejects it.**

**L7 limit:** if it grows DAGs/retries/matrices/dynamic registration it becomes a
workflow engine serving one consumer (§7 excludes those). Readable in one sitting
or the design was wrong.

---

## 3. Contract

### 3.1 Three ref classes — writer identity + protection for each (Pi #1)

| ref | writer | shape | protection |
|---|---|---|---|
| `refs/heads/jobs/queue` | **dispatcher App identity** | append-only, ff-only | ruleset: create+update restricted to the actor; force-push & delete denied |
| `refs/heads/jobs/status` | **box App identity** | append-only, ff-only, one stream keyed by `id` | ruleset: create+update restricted to the box actor; force & delete denied |
| `refs/heads/jobs/runner` | **box App identity** | **mutable single-writer snapshot** (see §4.5) | ruleset: update restricted to the box actor; delete denied; force-update *allowed for this ref only* |

All three are authoritative surfaces, so **all three** get a declared authenticated
writer and a **repository ruleset** — not just the queue. Rulesets must cover
**initial creation** as well as updates, deny **force-push and deletion** (except
`jobs/runner`'s own in-place snapshot update), and admit **no broad bypass actor**.
GitHub deploy keys are repo-scoped, not ref-scoped, so identity is a **dedicated
GitHub App installation** per role (dispatcher vs box), enforced by rulesets. An
acceptance test proves an unauthorized credential can neither create, update,
force, nor delete any operational ref (AC12).

**Why git, not a broker; why not the r0 boxes** — unchanged from v0.3.0 (git
already present; memory must not silently become a command channel, #690).

### 3.2 Job request — one frozen id grammar (Pi #1)

```yaml
schema: cmp.job-request.v1
id:      20260803T182204Z-slicer-suba-01     # THE id grammar (below); no colons
job:     slicer                              # registry key; never a command
job_version: "1"
params: { mode: sample, n: 60000 }
```

- **Frozen id grammar:** `^[0-9]{8}T[0-9]{6}Z-[a-z0-9][a-z0-9-]{0,31}$` — a
  UTC basic-format timestamp, `-`, then a slug ≤ 32 chars. **Total ≤ 47 chars**, so
  `cmpjob-<id>.service` (≤ 63-char systemd unit-name limit) and filesystem paths
  are always valid. One grammar only (no "or ULID"); the box rejects any other
  form before accepting.
- **`job` selects a registry key**, never a command/path/argv.
- **Binding** (with §3.6): the status entry carries the queue commit SHA, the
  canonical request digest, registry job id+version, and the authenticated App
  actor. A duplicate `id` with a **differing** request digest is an integrity
  violation → rejected (AC11), distinct from a benign replay (§3.5).

### 3.3 Registry — box-local, binds provenance + sandbox

`/etc/cmp-jobs/registry.yaml`, root-owned, 0644, **not in any repo**:
```yaml
jobs:
  slicer:
    version:     "1"
    exec:        /opt/cmp/impl/slicer/1/slicer-run   # immutable, versioned
    exec_sha256: <digest>                            # verified before every launch
    workdir:     /var/lib/cmp/slicer/work            # applied via WorkingDirectory (§4.1)
    input_roots:  [/var/lib/cmp/corpus]              # read-only
    output_roots: [/var/lib/cmp/slicer/out, /var/lib/cmp/slicer/state]
    network:     none
    user:        cmpjob
    memory:      10G
    cpu:         600%
    timeout_cap: 6h                                   # hard ceiling (§4.3)
    sandbox:     { NoNewPrivileges: true, CapabilityBoundingSet: "", ProtectSystem: strict, PrivateTmp: true }
    params:      { mode: [sample, full], n: { type: int, max: 400000 } }
```
The request selects an **immutable registered implementation** (digest verified),
never a mutable pathname. Changing what the box will run requires box access,
never a git push.

### 3.6 Wire format, canonicalization, cursor law (Pi #3)

Content addressing is only reproducible if the bytes are frozen:

- **Queue layout:** one **immutable request file per id** at
  `requests/<id>.json`, **one request added per commit** to `jobs/queue`. A commit
  that adds >1 request, mutates/deletes an existing request file, or is a **merge**
  is rejected as malformed (recorded to `jobs/status`, not silently skipped).
- **Cursor law:** the box advances a durable cursor by **first-parent linear
  traversal** from the last accepted `jobs/queue` commit; each new commit's single
  added `requests/<id>.json` is the next request. Non-linear history halts
  admission with `QUEUE-NONLINEAR` (a falsifiable stop, not silence).
- **Canonical digest:** `request_digest = SHA-256( exact bytes of requests/<id>.json )`
  — hash the admitted bytes verbatim (no re-serialization), and require the file to
  parse as UTF-8 JSON against `cmp.job-request.v1`. (Hashing exact bytes avoids
  canonicalization-algorithm ambiguity entirely.)
- **Status-event schema** (`cmp.job-status.v1`), one appended line per transition:
  `{ event_id, job_id, seq (per-job monotonic), state, request_digest,
  queue_commit, unit (cmpjob-<id>.service), actor, observed_at }`. Terminal states
  (`succeeded|failed|rejected|timeout`) are **final**; any later event for that
  `job_id` except an idempotent repeat is invalid.

### 3.4 Lifecycle — three distinct signals (Pi #6, prior)

```
queued → admitted → running ⇄ progress → { succeeded | failed | rejected | timeout }
```
- **poller liveness** = `jobs/runner` snapshot (§4.5).
- **job progress** = an **untrusted** record the job emits via a local file/socket,
  **relayed** (not authored) by the poller into `jobs/status`. A job may report
  progress; **a job may never declare its own terminal success.**
- **authoritative terminal** = the **systemd unit exit**, observed and published by
  the poller. Progress carries a monotonic counter + timestamp so *stalled* ≠
  *running*.

### 3.5 Exactly-once — atomic ledger contract (Pi #6)

A named ledger, not just components:

- **State:** root-owned `/var/lib/cmp-jobs/ledger/` with one record per
  `request_digest`. It enforces **two** uniqueness keys: `job_id → request_digest`
  (a reused id must carry the same digest) **and** `request_digest` itself.
- **Pre-launch intent:** before calling `systemd-run`, the box **atomically**
  writes an `INTENT{request_digest, id, unit, queue_commit}` record
  (`open(O_CREAT|O_EXCL)` → `write` → `fsync` → `fsync(dir)`); the create-exclusive
  is the launch lock. The **systemd unit name `cmpjob-<id>`** is the second lock: a
  start against a live/known unit is refused by systemd.
- **Reconciliation** (on poller restart, before any launch) inspects ledger + unit
  state and resolves every crash position:
  - INTENT present, **no unit** → crash *before* start → launch is permitted once.
  - INTENT present, unit **active** → crash *after* start → adopt, do not relaunch.
  - INTENT present, unit **exited & collected**, no terminal published → publish the
    reconciled terminal from the unit's recorded result; never relaunch.
  - terminal already in ledger → replay rejected.
- **Admissible transitions** are enumerated; anything else halts with
  `LEDGER-INCONSISTENT`. AC4 injects a fault **on both sides** of the start call
  (before-start and after-start-before-publish) — one case is insufficient.

---

## 4. Execution

### 4.1 One container

```
systemd-run --unit=cmpjob-<id> --collect --uid=cmpjob
  --property=WorkingDirectory=<registry.workdir>
  --property=MemoryMax=<memory> --property=MemorySwapMax=0
  --property=CPUQuota=<cpu>      --property=RuntimeMaxSec=<derived §4.3>
  --property=NoNewPrivileges=yes --property=CapabilityBoundingSet=
  --property=ProtectSystem=strict --property=PrivateTmp=yes
  --property=ReadOnlyPaths=<input_roots> --property=ReadWritePaths=<output_roots>
  --property=IPAddressDeny=any             # unless registry network != none
  -- <registry.exec, sha-verified> @<canonical param file, fd-passed §4.2>
```
cgroup limits (runaway dies in its own cgroup), survives its parent, journald
capture, `systemctl show` authoritative, and a **true per-job `memory.peak`** the
poller records into terminal status (the delegated per-stage peak Slicer Sub A
deferred to Sub B — supplied *once this layer is built and proven*, §9).

### 4.2 Identity + hardened root launcher (Pi #4)

- **Jobs run as `cmpjob`** — no sudo/keys/git credential; never `sigma`.
- **Unprivileged poller** parses attacker-controlled queue bytes; a **minimal
  root-owned launcher** performs the privileged start. The launcher **does not
  trust the poller's file**: it independently
  1. re-resolves the **registry key + version** and reloads registry policy,
  2. re-verifies **`exec_sha256`** against the on-disk executable,
  3. re-validates the **canonical param digest + schema** it was handed,
  4. accepts params only as a **protected spool object or an already-open file
     descriptor** (fd-passed), **never a caller-chosen path**; rejects symlinks,
     path traversal, and post-open replacement (**O_NOFOLLOW**, `fstat` ino/mode/uid
     checks, compare fd identity, TOCTOU-safe),
  5. requires the spool object be **root-owned, mode 0400**, on a non-world-writable
     dir.
- **Invocation authority is narrow and explicit:** a single systemd/Polkit action
  or one exact `sudoers` entry permitting only `cmp-launch <registry-key> <fd>` —
  not arbitrary argv, with a scrubbed environment (only registry-declared vars).
- Status is written by the poller, never the job (A10).

### 4.3 Timeout — refuse over-cap; bound the projection (Pi #5)

```
if projected_wall_s × safety > registry.timeout_cap:
    reject before launch  →  state: timeout_capacity_refused
else:
    RuntimeMaxSec = ceil(projected_wall_s × safety)   # within the cap
```
`min(cap, projected×safety)` was wrong — it silently admits jobs it predicts will
time out. **Projection source:** the admission layer accepts a projection **only**
if it is a bounded Slicer L1 **probe receipt** content-addressably bound to the
same `job_version`, input/corpus digest, scientific params, and `request_digest`.
If the box cannot verify that binding, **workload projection/refusal stays inside
Slicer** and the admission layer enforces **only the registry ceiling** — it never
accepts a self-asserted projection from the request.

### 4.4 Trigger

`systemd` timer, 60 s, polling `jobs/queue`. Not GitHub cron (drift/drops), not
push/webhook (inbound channel). Bounded, observable staleness.

### 4.5 `jobs/runner` — mutable snapshot, not a log (Pi #2)

An append-only 60 s heartbeat is ~525,600 commits/year while idle — telemetry
masquerading as an event log. `jobs/runner` is **current-state only**: a
**single-writer mutable ref force-updated in place** to a one-entry tree
`runner.json = { seq, observed_at, current_job|null, health, last_error }`, **no
retained per-minute history**. It stays **actor-restricted** (box App only) and
**CI-inert** (AC10). `jobs/queue` and `jobs/status` remain append-only; only this
telemetry ref is mutable. Poller fetch/API/ref failures set `health` to
`FETCH-FAIL`/`API-BLIND`/`REF-GONE` rather than reading as "no work."

---

## 5. Invariants (cannot be expressed)

| # | invariant | enforced by |
|---|---|---|
| A1 | a request cannot name an executable | `job` is a registry key |
| A2 | what the box runs cannot change via git | box-local root-owned registry; `exec_sha256` |
| A3 | a job cannot exceed its budget | registry cgroup/systemd properties |
| A4 | a job cannot escalate | `cmpjob`; NoNewPrivileges; empty caps |
| A5 | a job cannot run twice | atomic ledger (2 keys) + unit-name lock (§3.5) |
| A6 | a job cannot be orphaned | transient unit, independent lifetime |
| A7 | a job cannot declare its own success | terminal = systemd exit, poller-published |
| A8 | stalled ≠ running | monotonic progress counter + timestamp |
| A9 | dead poller ≠ idle box | `jobs/runner` snapshot + health codes |
| A10 | status cannot be forged by the job | poller writes; job has no git credential |
| A11 | no operational ref triggers a workflow | runtime CI-inert check on all three |
| A12 | root launcher never trusts poller input | independent re-resolve/verify; fd-only; TOCTOU-safe |
| A13 | no operational ref is writable/forgeable by an unauthorized actor | per-ref App identity + ruleset (create/update/force/delete) |
| A14 | request identity is reproducible | frozen wire bytes + exact-byte digest + linear cursor (§3.6) |
| A15 | a job predicted to exceed the cap is refused, not launched | `timeout_capacity_refused` (§4.3) |

---

## 6. Acceptance criteria (file + check + pass/fail)

| # | covers | pass iff |
|---|---|---|
| AC1 | A1,A2 | `job:/bin/sh`, unknown `job`, or a colon/oversized id → `rejected`; no unit created |
| AC2 | A3 | limits set from registry; induced OOM is `CONSTRAINT_MEMCG`; poller alive |
| AC3 | A4,A12 | uid=`cmpjob`; sudo denied; no key; git-write denied; launcher got only key+fd |
| AC4 | A5 | fault injected **before** start and **after start/before publish**: reconciliation relaunches neither; 0 recompute; replay rejected |
| AC5 | A6 | kill poller mid-job → unit stays `active`; poller reconciles terminal from unit exit |
| AC6 | A7,A10 | job exits nonzero and *tries* to self-publish success → poller publishes `failed`; self-success ignored |
| AC7 | A8 | `SIGSTOP` > 2× interval → progress counter stops; `jobs/runner` stays fresh; consumer flags `stalled` |
| AC8 | A9 | stop poller → `jobs/runner` stale/`FETCH-FAIL`; dead-poller ≠ idle-box |
| AC9 | §4.1,§4.3 | terminal records per-job `memory.peak` + `RuntimeMaxSec` |
| AC10 | A11 | push each of the 3 refs → **runtime Actions-API check**: 0 runs triggered |
| AC11 | A2 | duplicate `id`, different request digest → `rejected` (integrity), not replay |
| AC12 | A13 | an unauthorized credential cannot create/update/force/delete any of the 3 refs |
| AC13 | A14 | two independent processes derive the same `request_digest` and cursor position from `jobs/queue`; a merge/multi-add/mutating commit is rejected `QUEUE-NONLINEAR`/malformed |
| AC14 | A12 | launcher rejects a symlinked/replaced/world-writable param object and a path-not-fd invocation (TOCTOU) |
| AC15 | A15 | a request whose `projected×safety > cap` → `timeout_capacity_refused` before any unit start |

---

## 7. Deliberate non-goals

No retries, no DAG, no dynamic job registration, no log shipping, no
priorities/fairness/parallelism (one at a time, FIFO), no auto-escalation to an
agent.

---

## 8. Migration

1. Build alongside the workflow (no cutover).
2. Register `slicer` (versioned impl + `exec_sha256`).
3. **Prove AC1–AC15**, then dispatch one job **both ways** and compare receipts.
4. Switch the dispatcher to `jobs/queue` (dispatcher App identity).
5. **Unregister the self-hosted runner** — deletes the class.
6. Retire `slicer-status/**` for the `jobs/status` stream.

Rollback before step 5 = re-enable the workflow trigger.

---

## 9. Decisions

- **Signing:** required now if transport is a repo-wide deploy key/PAT; deferrable
  only after per-role App identities + rulesets restrict each operational ref
  **and** requests bind actor + queue commit SHA + exact-byte request digest.
  Trigger is a broad/shared credential, not a second dispatcher.
- **Queue-ref repo:** `usurobor/cmp` for v0; extract only for a second execution
  host/security domain or a non-CMP consumer.
- **Poller code home:** `cmp` until a second consumer appears.
- **Adoption gate:** this design cannot discharge Slicer's implementation ACs. The
  admission layer must be **built and proven through AC1–AC15** before it supplies
  per-job cgroup evidence to Sub B. Implementation-cell dispatch and cutover are
  **not** authorized by this document.

---

## 10. Cost

A poller (~150 lines) + a minimal root launcher, a registry schema, a status
writer + status/queue/runner ref-writers, an atomic admission ledger, per-role App
identities + rulesets, one new user. No broker, no daemon, no database. If the
implementation much exceeds ~300 lines, re-review rather than push through.
