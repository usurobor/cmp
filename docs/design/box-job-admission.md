# Box job admission — design

**Status:** `v0.1.0` — **design only, nothing built.** For review before any code.
**Author:** ω (Omega), operator's agent, outside the cell.
**Applies:** cnos.eng `evolve` (boundary moves), `code` (make invalid states harder
to express), `process-economics` (every step earns its cost), core `design`
(hide volatile decisions behind stable contracts), L7 (`ENGINEERING-LEVELS.md`).

---

## Authority — what this does and does not own

This document owns **how work arrives at the box and what admits it for
execution**. It does **not** own what the work is.

| | owner |
|---|---|
| stages, memory model, checkpointing, receipts, retrieval algorithm | **Slicer** (`compute-pipeline-architecture.md` + `slicer-issue-draft.md`) |
| transport, trust boundary, trigger, execution container, idempotence at the job level | **this document** |

Slicer's L1–L6 invariants stand unchanged and are **not** restated here. Where
this document and Slicer differ on anything executable inside a job, **Slicer
supersedes**.

This is deliberately narrow. The first draft of this design was a general "job
runner", which would have reintroduced Slicer's stage runner under a second name
— the failure the core `design` skill names explicitly ("the same concept is not
reintroduced under a second name somewhere else"). Slicer already anticipates
this split: its stages are *"transforms that do not care what invokes them."*
This document is what invokes them.

---

## 1. Problem

### 1.1 The recurring class

Not "a job failed." The class is **ad-hoc invocation with no admission
contract** — every job reaching the box does so by a bespoke path, with bespoke
(or absent) limits, identity, status, and replay semantics. Each one therefore
fails in a new way, and each fix is local.

Evidence from 2026-08-01→03, all on one box, all distinct mechanisms:

| # | failure | why invocation caused it |
|---|---|---|
| 1 | 21 abandoned agent sessions exhausted RAM; SSH appeared to "hang" | nothing bounded process lifetime; invocation had no owner |
| 2 | `gen_candidate_pairs` at 1.7 GB triggered a **global** OOM, killing the runner as collateral — twice | no per-job cgroup; the kernel chose a victim system-wide |
| 3 | processes survived cancelled runs | job lifetime not bound to a supervised unit |
| 4 | encode wrote into the CI workspace, one `git clean -ffdx` from deletion | output path was per-script convention, not contract |
| 5 | encode ran 2h20m at 3.2% with no observer | progress was per-script, absent by default |
| 6 | 5× "fetch failure indistinguishable from no data" | status/observation hand-rolled per consumer |
| 7 | every run unresumable | replay semantics per-script |

Slicer fixes 4–7 *for Slicer*. It does not fix 1–3, and it does not stop the
next script from repeating 4–7, because those are properties of **how a job is
invoked**, not of what the job computes.

### 1.2 The trust problem, which is new

Until 2026-08-03 the invocation path was a **self-hosted GitHub Actions runner**.
That means *GitHub decides what executes on the box*. While `cmp` was public,
this was a live compromise path:

```
fork → PR approved once (policy was first_time_contributors)
     → subsequent PRs run automatically as `sigma`
     → `sigma` has NOPASSWD sudo ALL
     → root on the box holding the 16 GB corpus
```

Mitigated on 2026-08-03 by tightening approval to `all_external_contributors`
and making `cmp` private again. But the *shape* survives: an inbound execution
channel exists, and its safety depends on repo visibility and approval
discipline rather than on structure.

### 1.3 What changes if this is not fixed

Every future consumer of box compute — Slicer, Pi, a future agent — either
inherits the self-hosted runner (and its trust properties), or hand-rolls its
own invocation and repeats §1.1.

---

## 2. The boundary move (why this is L7, and why it might not be)

**L7 test:** *"changes the system boundary so a whole class of future work gets
easier or disappears."*

The move: **invert control of invocation.** Today GitHub pushes work onto the
box. Instead, the box **pulls** work it decides to accept.

What disappears, structurally rather than by discipline:

- **No inbound execution channel.** Nothing outside can cause the box to run
  anything. Repo visibility, fork-PR policy, and runner registration stop being
  security-relevant. The self-hosted runner can be unregistered entirely.
- **Job status refs stop being dangerous.** `slicer-issue-draft` §164 already
  had to legislate *"`slicer-status/**` must trigger no workflow (else each
  heartbeat could re-dispatch and consume the very runner it reports on)."* When
  refs trigger nothing, that hazard cannot exist and the rule becomes unnecessary.
- **Limits, identity, and lifetime become structural**, not per-script — one
  execution container, applied uniformly, so §1.1 items 1–3 cannot recur.
- **Slicer becomes invocation-portable**, which its own design already wants.

**Where this could fail (the stated L7 limit — over-abstraction):** if it grows
into a general workflow engine — DAGs, retries, matrices, dynamic registration —
it becomes system weight serving one consumer. §6 lists what is deliberately
excluded to prevent that. The whole admission layer should be small enough to
read in one sitting; if it is not, this design was wrong.

---

## 3. Contract

### 3.1 Two refs, one direction each

```
refs/heads/jobs/queue        dispatcher writes; box reads      — requests
refs/heads/jobs/status/<id>  box writes; anyone reads          — lifecycle
```

Both orphan, append-only, fast-forward-only. Single writer each — the same
invariant the r0 boxes already use (cnos#690), so this introduces no new
concept.

**Why git and not a queue system:** both sides already have git, credentials,
and durability. A message broker would add infrastructure, secrets, and a
failure domain to serve one consumer at ~1 job/hour. `process-economics`: the
step must earn its cost, and at this volume it does not.

**Why a separate ref and not the r0 boxes:** an r0 box is *memory*. cnos#690's
promotion boundary is explicit — a channel entry governs nothing until promoted.
If the executor acted on r0 entries, memory would silently become a command
channel. Separate ref, separate meaning, protected independently.

### 3.2 Job request

```yaml
id:      2026-08-03T18:22:04Z-slicer-suba-01   # unique, immutable, dispatcher-assigned
job:     slicer                                 # SELECTS a known job; never a command
params:
  mode:  sample
  n:     60000
dispatcher: sigma/cloud
```

`job` is a **key into a box-local registry**, never a command string, path, or
argv. A request cannot express "run this"; only "run the thing you already call
`slicer`, with these parameters."

### 3.3 The registry is box-local and not pushable

**The single most important property in this design.**

```
/etc/cmp-jobs/registry.yaml     root-owned, 0644, NOT in any repo
```

```yaml
jobs:
  slicer:
    exec:    /opt/cmp/bin/slicer-run
    user:    cmpjob
    memory:  10G
    cpu:     600%
    timeout: 6h
    params:  {mode: [sample, full], n: {type: int, max: 400000}}
```

If the registry lived in the repo, anyone who can push could add a job — and the
trust boundary would collapse back to "whoever can write git can run code as
root." Because it is box-local and root-owned, **changing what the box is willing
to do requires access to the box**, which is exactly the property we want.

Parameters are **validated against a declared schema** before execution.
Unknown job name, unknown parameter, or out-of-range value ⇒ rejected, with the
rejection published to status. Invalid states are unrepresentable rather than
merely discouraged (`eng/code`).

### 3.4 Lifecycle

```
queued → admitted → running ⇄ heartbeat → { succeeded | failed | rejected | timeout }
```

Each transition appends a typed entry to `jobs/status/<id>`. Terminal states are
final; there is no transition out of them.

**Heartbeat carries a monotonic counter and a timestamp.** This is what makes
*stalled* distinguishable from *running* and from *done* — the distinction whose
absence cost 2h20m of an encode at 3.2% with nobody able to tell. A consumer
that sees no heartbeat for > 2× the interval knows the job is stalled; it does
not have to infer it from silence.

This is Slicer's L3 and ω's rule — **silence must be falsifiable** — applied at
the admission layer rather than re-implemented per job.

---

## 4. Execution

### 4.1 One container, uniformly applied

Every admitted job runs as a **systemd transient unit**:

```
systemd-run --unit=cmpjob-<id> --collect
            --uid=cmpjob
            --property=MemoryMax=<registry>
            --property=CPUQuota=<registry>
            --property=RuntimeMaxSec=<registry>
            -- <registry.exec> <validated params>
```

Rationale, each point earned this week:

- **cgroup limits** — a runaway dies inside its own cgroup. Already proven on
  this box: after `MemoryMax`, the next OOM was `CONSTRAINT_MEMCG` and the runner
  survived, where two earlier global OOMs had killed it.
- **survives its parent** — an SSH drop or poller restart cannot orphan or kill a
  running job (§1.1 item 3).
- **journald captures stdout/stderr** — no log shipping to build; no
  `2>/dev/null` (Slicer L5).
- **`systemctl show` is the authoritative run state** — the box does not have to
  track liveness itself.
- **`RuntimeMaxSec`** — a wedged job dies rather than holding the box.

### 4.2 Identity

Jobs run as **`cmpjob`: a dedicated user with no sudo, no SSH key, no git write
credential.**

Explicitly **not `sigma`**, which holds `NOPASSWD sudo ALL`. That grant is what
turned §1.2's fork-PR path from "untrusted code runs" into "untrusted code
becomes root." A job needs to read the corpus and write its outputs; it does not
need root, and giving it root couples job compromise to box compromise.

Status is published by the **poller**, not the job — so a job cannot forge its
own success.

### 4.3 Trigger

A `systemd` timer, 60s, polling the queue ref.

**Not GitHub cron:** unreliable under load (10–30 min drift observed, sometimes
dropped) — the specific unreliability that motivated this whole redesign.
**Not push/webhook:** that is an inbound channel, which §2 exists to remove.

Polling is O(1) network per minute and the failure mode is bounded staleness,
which is observable. The 60s timer already runs on this box and has been
reliable across a reboot and a resize.

---

## 5. Invariants

Stated as things that **cannot be expressed**, not things we intend to avoid:

| # | invariant | enforced by |
|---|---|---|
| A1 | A request cannot name an executable | `job` is a registry key; no command strings accepted |
| A2 | What the box will run cannot be changed via git | registry is box-local, root-owned |
| A3 | A job cannot exceed its declared budget | cgroup properties from the registry, applied by the container |
| A4 | A job cannot escalate | `cmpjob`: no sudo, no keys, no git credential |
| A5 | A job cannot run twice | terminal status for `id` ⇒ replay rejected |
| A6 | A job cannot be orphaned by its dispatcher | transient unit, independent lifetime |
| A7 | A job cannot fail silently | poller publishes terminal state; unit exit is authoritative |
| A8 | A stalled job is distinguishable from a running one | monotonic heartbeat + timestamp |
| A9 | An unreachable observer is distinguishable from an idle one | poller emits its own failure modes (`API-BLIND`, `REF-GONE`, `FETCH-FAIL`) |
| A10 | Status cannot be forged by the job | poller writes status; job has no git credential |

A9 exists because it was violated five times in three days, each time by a
different mechanism, and each time absence-of-signal was read as absence-of-event.

---

## 6. Deliberate non-goals

Excluded to keep this from becoming a workflow engine (the L7 over-abstraction
failure). Each may be added later **only when a concrete need exists**:

- **No retries.** A failed job publishes cause and stops. Retrying an unknown
  failure wastes a 2-hour encode; deciding requires judgement.
- **No dependency graph / DAG.** Slicer sequences its own stages.
- **No dynamic job registration.** Adding a job is a deliberate box-side edit.
- **No log shipping.** journald on the box; status carries a tail on terminal.
- **No priorities, no fairness, no parallelism across jobs.** One at a time,
  FIFO. Concurrency is what caused contention; add it when measured, not assumed.
- **No auto-escalation to an agent.** A failure never invokes an LLM with root.
  It publishes, and a human or ω decides. This is the boundary that keeps the
  natural-language trust surface off the box.

---

## 7. Migration

Slicer's internals are untouched. Only its invocation changes.

1. **Build the admission layer alongside** the existing workflow. No cutover.
2. **Register `slicer`** in the box registry, pointing at the entrypoint Slicer's
   issue already defines.
3. **Dispatch one job both ways** — via the existing workflow and via the queue
   ref — and compare receipts. Equivalence is the gate.
4. **Switch `sigma/cloud` to dispatch via the queue ref.**
5. **Unregister the self-hosted runner from `cmp`.** This is the step that
   actually deletes the class; until it happens, the inbound channel still exists.
6. **Retire `slicer-status/**`** in favour of `jobs/status/<id>` — one convention,
   and it removes the "status refs must not trigger workflows" rule with it.

Rollback at any point before 5 is re-enabling the workflow trigger.

---

## 8. Open questions — for review, not assumed

1. **Who may write `jobs/queue`?** Today `sigma/cloud` uses the same token as
   everything else. A dedicated deploy key scoped to that ref would make "who can
   make the box compute" independently answerable. Worth it, or premature?
2. **Does the box verify commit signatures on the queue ref?** Strongest form of
   A2's spirit, but adds key management for a single dispatcher.
3. **Is `jobs/queue` in `cmp`, or its own repo?** In `cmp` is cohesive today;
   its own repo if Pi or others later dispatch. Recommend `cmp` now, extract when
   a second dispatcher appears.
4. **Where does this code live?** `cmp` today (single consumer). If a second
   consumer appears it should be extracted — that is the trigger, not a guess.
5. **Does ω own the poller, or is it unowned infrastructure?** It needs no
   judgement, so it should be unowned. But someone must notice when *it* breaks,
   and that is ω's watcher — which must therefore not depend on it.
6. **Timeout policy per job class.** `RuntimeMaxSec=6h` is a guess; a full-corpus
   encode may legitimately exceed it. Slicer's capacity probe (L1) could supply
   the number instead of a constant.

---

## 9. What this costs

Roughly: a poller (~150 lines), a registry schema, a status writer, a systemd
timer, and one new user. No new services, no broker, no daemon, no database.

Against: deleting an inbound execution channel, removing the repo-visibility
dependency, and making §1.1 items 1–3 structurally impossible for every future
consumer of box compute — not just Slicer.

If the implementation exceeds roughly 300 lines, the design was wrong and should
be re-reviewed rather than pushed through.
