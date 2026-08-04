# Box job admission — design

**Status:** `v0.3.0` — **design only, nothing built.** Converged draft after Pi
review (`CHANGES_REQUESTED` on v0.2.0); adopts all ten of Pi's blocking
corrections and its two open-question decisions.
**Design:** ω (Omega). **Finalized/converged:** δ (cn-sigma@cmp:claude/chat).
**Applies:** cnos.eng `evolve`, `code` (make invalid states unrepresentable),
`process-economics`, core `design`, `performance-reliability`, L7.

**v0.3.0 changelog (Pi corrections):** ref-safe job-id grammar (§3.2); one
append-only `jobs/status` stream, not a per-job branch family (§3.1); transport
identity is a GitHub App + ref ruleset, not a "ref-scoped deploy key" (§3.1);
requests are content-addressed and writer-authenticated (§3.2); exactly-once via
a box-local admission ledger + unit-name lock (§3.5); unprivileged poller + minimal
root launcher (§4.2); registry binds executable provenance + sandbox policy
(§3.3); job progress split from poller liveness and from authoritative terminal
state (§3.4); all operational ref families proven CI-inert at runtime (§5, AC10);
security claim narrowed (§2, §5). Signing decision resolved (§9).

---

## Authority — what this does and does not own

Owns **how work arrives at the box and what admits it**. Does **not** own what the
work is.

| | owner |
|---|---|
| stages, memory model, checkpointing, receipts, algorithm | **Slicer** |
| transport, trust boundary, trigger, execution container, job-level idempotence | **this document** |

Where this and Slicer differ on anything executable inside a job, **Slicer
supersedes**. This is deliberately narrow — not a workflow engine, not a second
stage runner.

---

## 1. Problem

### 1.1 The recurring class

The class is **ad-hoc invocation with no admission contract** — every job reaches
the box by a bespoke path with bespoke (or absent) limits, identity, status, and
replay. Each fails a new way; each fix is local. Evidence, 2026-08-01→03, one box:

| # | failure | why *invocation* caused it |
|---|---|---|
| 1 | 21 abandoned sessions exhausted RAM; SSH "hung" | nothing bounded process lifetime |
| 2 | 1.7 GB job triggered a **global** OOM, killed the runner — twice | no per-job cgroup |
| 3 | processes survived cancelled runs | job lifetime not bound to a supervised unit |
| 4 | encode wrote into the CI workspace, near `git clean -ffdx` | output path was convention, not contract |
| 5 | encode ran 2h20m at 3.2% with no observer | progress per-script, absent by default |
| 6 | 5× "fetch failure indistinguishable from no data" | status hand-rolled per consumer |
| 7 | every run unresumable | replay per-script |

Slicer fixes 4–7 *for Slicer*; it does not fix 1–3, and nothing stops the next
script repeating 4–7 — those are properties of **how a job is invoked**.

### 1.2 The trust problem

Until 2026-08-03 the invocation path was a self-hosted GitHub Actions runner —
*GitHub decides what executes on the box*. While `cmp` was public:
```
fork → PR approved once → later PRs auto-run as `sigma` → `sigma` NOPASSWD sudo ALL → root on the corpus box
```
Mitigated by tightening approval and re-privatising `cmp`, but the *shape*
survives: an inbound execution channel whose safety rests on repo visibility and
approval discipline, not structure. (Visibility has since flipped again for the
comms bridge — proof that "safe because private" is not a structural guarantee.)

### 1.3 If unfixed

Every future consumer of box compute inherits the runner's trust properties or
hand-rolls invocation and repeats §1.1.

---

## 2. The boundary move (L7)

**Invert control of invocation.** Today GitHub pushes work onto the box; instead
the box **pulls** work it decides to accept.

What changes structurally, not by discipline:
- **No inbound *code* channel.** No outside party can supply code, command,
  executable path, or resource policy. The self-hosted runner can be unregistered.
- **Status refs stop being dangerous** — when refs trigger no workflow, the
  "status must not re-dispatch" hazard cannot exist.
- **Limits, identity, lifetime become structural** (one container), so §1.1 1–3
  cannot recur.
- **Slicer becomes invocation-portable.**

**Narrowed security claim (Pi #10).** Not "nothing outside can cause the box to
run anything" — an authorized queue writer *can* request a locally-registered
job. The warranted claim is: **no outside party can supply code, command,
executable path, or resource policy; an outside actor may only *request* a known
registered job, and the box's local policy independently admits or rejects it.**

**Where this fails (the L7 limit):** if it grows DAGs/retries/matrices/dynamic
registration it becomes a workflow engine serving one consumer. §7 excludes those.
Small enough to read in one sitting, or the design was wrong.

---

## 3. Contract

### 3.1 Three ref classes, single writer each (Pi #2)

```
refs/heads/jobs/queue     dispatcher writes; box reads   — requests (append-only)
refs/heads/jobs/status    box writes; anyone reads       — ONE lifecycle stream, keyed by id
refs/heads/jobs/runner    box writes; anyone reads        — poller liveness (§3.4)
```

All orphan, append-only, fast-forward-only, never deleted (proxy blocks deletion
anyway; terminal states are tombstones). **`jobs/status` is one stream** whose
entries are keyed by `id` — *not* a `jobs/status/<id>` branch family, which would
mint thousands of permanent refs. A reader reconstructs one job's lifecycle by
filtering the stream on `id` (same model as #698 thread reconstruction).

**Transport identity (Pi #3).** A GitHub **deploy key is repository-scoped, not
ref-scoped** — its API exposes only `read_only`, no ref pattern. So writer
identity + ref restriction is: a **dedicated GitHub App installation identity**
for the dispatcher, plus a **repository ruleset** restricting updates to
`refs/heads/jobs/queue` to that actor. A PAT/deploy key MUST NOT be described as
ref-scoped unless a ruleset (or custom git proxy) actually enforces it.

**Why git, not a broker:** both sides already have git, creds, durability; a
broker adds infra/secrets/a failure domain to serve ~1 job/hour. **Why not the r0
boxes:** an r0 box is *memory*; #690's promotion boundary means memory must not
silently become a command channel.

### 3.2 Job request — ref-safe id, content-addressed, authenticated (Pi #1, #4)

```yaml
schema: cmp.job-request.v1
id:      20260803T182204Z-slicer-suba-01   # ref-safe: [A-Za-z0-9._-] only, NO colons
job:     slicer                            # SELECTS a registered job; never a command
job_version: "1"                           # pins the registry entry version
params: { mode: sample, n: 60000 }
```

- **Ref-safe id grammar (Pi #1).** `^[0-9]{8}T[0-9]{6}Z-[a-z0-9-]+$` (a ULID/UUIDv7
  is also acceptable). Colons are forbidden — they cannot appear in a git ref, and
  the old example `2026-08-03T18:22:04Z-…` could not have produced its own status
  entry. The box **mechanically validates** the id before accepting.
- **`job` is a registry key** — never a command, path, or argv.
- **Binding (Pi #4).** On admission the box records, and the status entry carries:
  the **queue commit SHA**, a **canonical digest of the request payload**, the
  **registry job identity + version**, and the **authenticated writer identity**
  (from the App/ruleset actor, not self-asserted text). A duplicate `id` with a
  **differing payload digest is an integrity violation → rejected**, distinct from
  a benign replay (§3.5). `dispatcher:` free text is advisory only.

### 3.3 Registry — box-local, binds provenance + sandbox (Pi #8)

```
/etc/cmp-jobs/registry.yaml     root-owned, 0644, NOT in any repo
```
```yaml
jobs:
  slicer:
    version:     "1"
    exec:        /opt/cmp/impl/slicer/1/slicer-run   # immutable, versioned path
    exec_sha256: <digest>                             # provenance: bound, verified before launch
    workdir:     /var/lib/cmp/slicer/work
    input_roots:  [/var/lib/cmp/corpus]               # read-only mounts
    output_roots: [/var/lib/cmp/slicer/out, /var/lib/cmp/slicer/state]
    network:     none
    user:        cmpjob
    memory:      10G
    cpu:         600%
    timeout_cap: 6h            # hard ceiling; effective timeout derived (§4.3)
    sandbox:     { NoNewPrivileges: true, CapabilityBoundingSet: "", ProtectSystem: strict, PrivateTmp: true }
    params:      { mode: [sample, full], n: { type: int, max: 400000 } }
```

The request **selects an immutable registered implementation**, not a mutable
pathname: the box verifies `exec_sha256` before launch, so `exec` cannot change
underneath an old request. Allowed input/output roots, network policy, and
systemd hardening are registry-declared, not job-chosen. Box-local + root-owned
means **changing what the box will run requires box access**, never a git push.
Params validated against the schema; unknown/out-of-range ⇒ rejected to status.

### 3.4 Lifecycle — three distinct signals (Pi #6)

```
queued → admitted → running ⇄ progress → { succeeded | failed | rejected | timeout }
```
Separate what were conflated:
- **poller liveness** — `jobs/runner`, monotonic seq + timestamp + `current_job|null`.
  Says the *poller* is alive, independent of any job.
- **job progress** — an **untrusted** record the job emits via a local file/socket,
  **relayed** (not authored) by the poller into `jobs/status`. A job may report
  progress; **a job may never declare its own terminal success.**
- **authoritative terminal state** — the **systemd unit exit**, observed by the
  poller (`systemctl show`) and published by it.

Progress carries a monotonic counter + timestamp so *stalled* is distinguishable
from *running* (the 2h20m-at-3.2% failure). This is Slicer's L3 / ω's rule —
**silence must be falsifiable** — at the admission layer.

### 3.5 Exactly-once — box-local ledger + unit lock (Pi #5)

"Terminal status exists ⇒ replay rejected" does not close the crash window where
the poller starts a unit and dies before publishing `admitted/running`; on restart
it could launch again. So:
- a **root-owned admission ledger** keyed by **request digest** records every
  admit/launch/terminal transition locally, before and independent of the ref push;
- the **systemd unit name is the idempotency lock** (`cmpjob-<id>`): a start with a
  live/known unit name is refused by systemd itself;
- **restart reconciliation** inspects **ledger + unit state before any launch** and
  publishes the reconciled terminal state.

Exactly-once is enforced by ledger+unit locally; the ref is the *report*, not the
lock.

---

## 4. Execution

### 4.1 One container, uniformly applied

```
systemd-run --unit=cmpjob-<id> --collect --uid=cmpjob
  --property=MemoryMax=<registry.memory> --property=MemorySwapMax=0
  --property=CPUQuota=<registry.cpu>     --property=RuntimeMaxSec=<derived §4.3>
  --property=NoNewPrivileges=yes --property=CapabilityBoundingSet=
  --property=ProtectSystem=strict --property=PrivateTmp=yes
  --property=ReadOnlyPaths=<input_roots> --property=ReadWritePaths=<output_roots>
  --property=IPAddressDeny=any            # unless registry network != none
  -- <registry.exec, sha-verified> <validated canonical param file>
```
- **cgroup limits** — a runaway dies in its own cgroup (proven: `CONSTRAINT_MEMCG`,
  runner survived where two global OOMs had killed it).
- **survives its parent** — SSH drop / poller restart cannot orphan or kill it.
- **journald** captures stdout/stderr; no log shipping, no `2>/dev/null` (L5).
- **`systemctl show`** is authoritative run state.
- **per-job cgroup peak** — `memory.peak` of `cmpjob-<id>` is a true per-job
  high-water; the poller reads it into terminal status. This is the delegated
  per-stage peak Slicer Sub A deferred to Sub B — supplied here for free, **once
  this layer is built and proven** (§9 adoption gate).

### 4.2 Identity + poller/root split (Pi #7)

- **Jobs run as `cmpjob`** — no sudo, no SSH key, no git credential. Explicitly not
  `sigma` (NOPASSWD sudo). A job reads the corpus and writes its outputs; root
  would couple job compromise to box compromise.
- **The component that parses attacker-controlled queue bytes is unprivileged.**
  Split into: an **unprivileged poller** (fetch, validate, schedule, publish
  status) + a **minimal root-owned launcher** invoked only with a **validated
  registry key + a canonical parameter file** — never arbitrary argv. The launcher
  applies the systemd template and starts the unit. No root process ever parses raw
  request text.
- Status is written by the poller, never the job (A10).

### 4.3 Timeout — derived, not guessed

`RuntimeMaxSec = min(registry.timeout_cap, ceil(L1_projected_wall_s × safety))`,
`safety` declared (e.g. 1.5). Ties admission to measurement; a legitimate long job
isn't killed by a guessed constant, a wedged one still dies at the cap.

### 4.4 Trigger

A `systemd` timer, 60 s, polling the queue ref. Not GitHub cron (10–30 min drift,
sometimes dropped — the unreliability that motivated this). Not push/webhook
(inbound channel, which §2 removes). Failure mode is bounded, observable staleness.

---

## 5. Invariants

Stated as things that **cannot be expressed**:

| # | invariant | enforced by |
|---|---|---|
| A1 | a request cannot name an executable | `job` is a registry key; no commands accepted |
| A2 | what the box runs cannot change via git | registry box-local, root-owned; `exec_sha256` verified |
| A3 | a job cannot exceed its budget | registry cgroup/systemd properties |
| A4 | a job cannot escalate | `cmpjob`: no sudo/keys/git; `NoNewPrivileges`, empty caps |
| A5 | a job cannot run twice | admission ledger (request digest) + unit-name lock (§3.5) |
| A6 | a job cannot be orphaned by its dispatcher | transient unit, independent lifetime |
| A7 | a job cannot declare its own success | terminal state = systemd exit, published by poller |
| A8 | stalled is distinguishable from running | monotonic progress counter + timestamp |
| A9 | dead poller is distinguishable from idle box | `jobs/runner` liveness + `FETCH-FAIL`/`API-BLIND`/`REF-GONE` |
| A10 | status cannot be forged by the job | poller writes status; job has no git credential |
| A11 | no operational ref triggers a workflow | `jobs/queue`, `jobs/status`, `jobs/runner` proven CI-inert at runtime |
| A12 | the root launcher never parses raw request bytes | unprivileged poller + validated-key/param-file launcher |

---

## 6. Acceptance criteria (file + check + pass/fail)

Each demonstrated before cutover (§8 step 3).

| # | covers | setup | pass iff |
|---|---|---|---|
| **AC1** | A1,A2 | queue `job:/bin/sh` or unknown `job`, or an id with a colon | `rejected` w/ reason; **no** `cmpjob-<id>` unit ever created |
| **AC2** | A3 | queue `slicer` at `n` max, induce over-budget | `MemoryMax/CPUQuota/RuntimeMaxSec` set; OOM is `CONSTRAINT_MEMCG`; poller alive |
| **AC3** | A4,A12 | inspect running job + the root launcher | uid=`cmpjob`; sudo denied; no key; git-write denied; launcher got only key+param-file |
| **AC4** | A5 | re-queue a terminal `id`; and kill the poller *between unit start and status push* | replay `rejected`; reconciliation finds ledger+unit and does **not** relaunch; 0 recompute |
| **AC5** | A6 | kill poller mid-job | unit still `active`; job completes; poller reconciles terminal from unit exit |
| **AC6** | A7,A10 | job exits nonzero; job also *tries* to publish success | terminal `failed` published **by poller** w/ unit exit; job's self-success is ignored |
| **AC7** | A8 | `SIGSTOP` a running job > 2× interval | progress counter stops; consumer flags `stalled`; `jobs/runner` stays fresh |
| **AC8** | A9 | stop the poller | `jobs/runner` goes stale / `FETCH-FAIL`; reader distinguishes dead-poller from idle-box |
| **AC9** | §4.1,§4.3 | run `slicer` to completion | terminal records per-job `memory.peak` + derived `RuntimeMaxSec` |
| **AC10** | A11 | push to each of `jobs/queue`, `jobs/status`, `jobs/runner` | **runtime Actions-API check**: 0 workflow runs triggered (not just static text) |
| **AC11** | A2,§3.2 | queue a duplicate `id` with a different payload digest | `rejected` as integrity violation, not treated as replay |

---

## 7. Deliberate non-goals

No retries (a failed job publishes cause and stops; re-dispatch is a judgement
call). No DAG. No dynamic job registration. No log shipping. No priorities/
fairness/parallelism (one at a time, FIFO). No auto-escalation to an agent (a
failure never invokes an LLM with root).

---

## 8. Migration

1. Build the admission layer **alongside** the workflow (no cutover).
2. Register `slicer` (versioned impl + `exec_sha256`).
3. **Prove AC1–AC11**, then dispatch one job **both ways** (workflow + queue ref)
   and compare receipts — equivalence is the gate.
4. Switch the dispatcher to the queue ref (over the App/ruleset actor).
5. **Unregister the self-hosted runner** — the step that deletes the class.
6. Retire `slicer-status/**` in favour of the `jobs/status` stream.

Rollback before step 5 = re-enable the workflow trigger.

---

## 9. Decisions (Pi-resolved)

- **Signing trigger (§9-2 → resolved).** Signing is **required now** if transport
  is a repository-wide deploy key/PAT (broader than the queue ref; writer identity
  otherwise only asserted). It may be **deferred only after** a ruleset restricts
  `jobs/queue` to one dedicated **GitHub App** identity **and** requests bind
  actor + queue commit SHA + canonical payload digest. Trigger is a shared/
  insufficiently-scoped credential — **not** a second dispatcher.
- **Queue-ref repo (§9-3 → resolved).** Keep queue/status in `usurobor/cmp` for
  v0. Extract only for a second execution host / security domain, or a non-CMP
  consumer with a different authority/retention boundary — a second dispatcher
  alone changes authorization, not ownership.
- **Poller code home.** `cmp` until a second consumer appears.
- **Adoption gate.** This design cannot discharge Slicer's implementation ACs.
  The admission layer must be **built and proven through AC1–AC11** before it may
  supply per-job cgroup evidence to Sub B.

---

## 10. Cost

A poller (~150 lines) + a minimal root launcher, a registry schema, a status
writer, a systemd timer, an admission ledger, one App identity + ruleset, one new
user. No broker, no daemon, no database. Against: deleting the inbound execution
channel and making §1.1 1–3 structurally impossible for every future consumer of
box compute. If the implementation much exceeds ~300 lines, re-review rather than
push through.
