# Box job admission — design

**Status:** `v0.5.1` — **design only, nothing built.** Pi granted
**CONDITIONAL_APPROVAL** of v0.5.0 (architecture + transport converged) with four
mechanical errata; this version applies them exactly. Per Pi, once these exact
corrections are committed the **implementation cell is pre-authorized to dispatch
without another design review**.
**v0.5.1 errata:** (1) removed the stale "App actor" — status carries the
**verified signing principal + fingerprint**, and v0 key trust anchors/lifecycle
are frozen (§3.2); (2) **anti-rollback** for the box-signed status & runner refs —
`STATUS-TAMPER`/`RUNNER-REPLAY` (§3.6, §4.5, A16, AC16); (3) the **root launcher
independently verifies signed-queue provenance** via `cmp-launch <key> <queue-sha>
<id>` reading from a fixed bare mirror, trusting no poller path/bytes/signature-
decision (§4.2, AC14); (4) **`indeterminate`/`launch_outcome_unknown`** added as a
truthful terminal state to the lifecycle, schema, ledger table, and AC (§3.4/§3.5/
§3.6, AC17).
**Design:** ω (Omega). **Converged:** δ (cn-sigma@cmp:claude/chat).
**Operator decision folded in:** transport trust = **git-native signed objects**
(option B), not a GitHub org + rulesets — the box verifies signatures, aligned
with cnos's git-native model.
**Applies:** cnos.eng `evolve`, `code`, `process-economics`, core `design`,
`performance-reliability`, L7.

**v0.5.0 changelog (Pi's final 4):** transport is **signed git objects verified
against a box-local `allowed_signers`**, since `cmp` is a User repo where org
rulesets don't exist; ref write-access is *not* relied upon — unsigned/unknown-key
commits are never admitted (§3.1). Launch guarantee is honest **at-most-once**:
an ambiguous outcome is never auto-relaunched, and Slicer's durable resume carries
recovery (§3.5). Workload **projection/refusal stays inside Slicer**; the box
enforces only the root-owned registry ceiling — removing the circular
probe-receipt binding (§4.3). Wire holes closed: one exact-byte `request_digest`
(no separate "canonical param digest"); a frozen `jobs/status` physical layout;
a **queue-system event schema** for failures without a job id; `timeout_capacity_
refused` demoted to a `rejected` reason (§3.6).

**v0.4.0 changelog (Pi's 7):** all-three-ref writer roles; `jobs/runner` mutable
snapshot; frozen wire/cursor/status schema; single id grammar; hardened root
launcher; over-cap timeout; atomic ledger; ACs for the new trust claims.

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

### 3.1 Three ref classes — signature-gated, not ACL-gated (Pi #1)

`usurobor/cmp` is owned by a GitHub **User**, not an org, so per-ref actor
allowlists / bypass rulesets **do not exist** on this host. Transport trust is
therefore **git-native signed objects**, verified by the box — which fits cnos's
model (the box already trusts its own local policy, not GitHub's ACLs).

| ref | writer role | shape | trust gate (box-verified) |
|---|---|---|---|
| `refs/heads/jobs/queue` | **dispatcher key** | append-only, ff-only | commit **SSH-signed by a `dispatcher` key in the box-local `allowed_signers`**; else not admitted |
| `refs/heads/jobs/status` | **box key** | append-only, ff-only, `events/<event_id>.json`, one per commit (§3.6) | commit signed by the **box key**; readers verify; unsigned/other-key ⇒ not trusted |
| `refs/heads/jobs/runner` | **box key** | **mutable single-writer snapshot** (§4.5) | box-key-signed; force-update allowed for this ref only |

- **`allowed_signers` is box-local, root-owned** (same authority as the registry):
  changing who may dispatch requires **box access**, never a git push.
- **Ref write-access is NOT the trust boundary.** Anyone with push access can add
  a commit; the box **admits only commits signed by an allowed `dispatcher` key**
  (`git verify-commit` / `ssh-keygen -Y verify`). An unsigned or wrong-key commit
  is rejected `QUEUE-UNSIGNED` — **write-access ≠ admission**.
- **Tamper / rewrite:** the box pins `last_accepted_commit`; if the queue ref no
  longer descends from it (force-push/rewrite), admission halts `QUEUE-TAMPER`
  (falsifiable, not silent). Deletion is blocked by the proxy anyway.
- AC12 proves an unsigned or wrong-key request commit is never admitted, and a
  status commit not signed by the box key is never trusted.

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
  exact-byte request digest, registry job id+version, and the **verified signing
  principal + key fingerprint** — derived from signature verification, **never**
  from request text or git author metadata. A duplicate `id` with a **differing**
  request digest is an integrity violation → rejected (AC11), distinct from a
  benign replay (§3.5).
- **Trust anchors + key lifecycle (frozen for v0).** Two separate anchors, both
  root-owned and box-local: (a) **queue trust** = the dispatcher principal/key
  verified against `allowed_signers`; (b) **status/runner trust** = the box signing
  key, verified by external readers (δ) against a **separately pinned public
  key/fingerprint** — never material learned from the status/runner ref itself. No
  transparent key rotation: a key change is an explicit maintenance event that
  updates the relevant pinned trust material **before** any new object signed by
  the new key is accepted.

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
- **One digest only.** `request_digest = SHA-256(exact bytes of requests/<id>.json)`
  is the sole content address; the root launcher verifies **these exact bytes**
  (§4.2). There is no separate "canonical param digest" — the bytes are the param.
- **`jobs/status` physical layout (frozen, mirrors the queue).** One box-signed
  commit appends exactly one `events/<event_id>.json`; `event_id` is a
  left-zero-padded global monotonic counter (`000001`, `000002`, …). The reader's
  cursor is first-parent linear traversal, same law as the queue.
- **Anti-rollback (Pi erratum, A16).** A valid box signature also authenticates an
  *old* object, so signature validity is not freshness. Status readers pin
  `last_accepted_status_commit` **and** the last global `event_id`; a non-descendant
  ref, or a regressing/reused non-idempotent `event_id`, is `STATUS-TAMPER` and
  halts acceptance (falsifiable, not silent).
- **Two event kinds.**
  - **Job event** `cmp.job-status.v1`: `{ event_id, job_id, seq (per-job monotonic),
    state ∈ {queued,admitted,running,succeeded,failed,rejected,timeout,indeterminate},
    reason?, request_digest, queue_commit, unit, principal, fingerprint,
    observed_at }`. Terminal (`succeeded|failed|rejected|timeout|indeterminate`) is
    **final**; any later non-idempotent event for that `job_id` is invalid.
    `timeout_capacity_refused` is **not** a state — it is `state: rejected, reason:
    timeout_capacity_refused`. **`indeterminate`** (reason `launch_outcome_unknown`)
    is the truthful terminal for a launch whose outcome cannot be established
    (§3.5) — never encoded as `failed`/`rejected`; final, never auto-relaunched.
  - **Queue-system event** `cmp.queue-event.v1`, for failures with **no valid job
    id/digest** (a malformed/multi-add/mutating commit, `QUEUE-NONLINEAR`,
    `QUEUE-UNSIGNED`, `QUEUE-TAMPER`, `LEDGER-INCONSISTENT`):
    `{ event_id, kind, queue_commit|ref_position, detail, observed_at }` with
    `job_id`/`request_digest` **null**. This is the cursor/error identity for
    queue-level faults.

### 3.4 Lifecycle — three distinct signals (Pi #6, prior)

```
queued → admitted → running ⇄ progress → { succeeded | failed | rejected | timeout | indeterminate }
```
- **poller liveness** = `jobs/runner` snapshot (§4.5).
- **job progress** = an **untrusted** record the job emits via a local file/socket,
  **relayed** (not authored) by the poller into `jobs/status`. A job may report
  progress; **a job may never declare its own terminal success.**
- **authoritative terminal** = the **systemd unit exit**, observed and published by
  the poller. Progress carries a monotonic counter + timestamp so *stalled* ≠
  *running*.

### 3.5 At-most-once launch — atomic ledger contract (Pi #6)

A named ledger, not just components:

- **State:** root-owned `/var/lib/cmp-jobs/ledger/` with one record per
  `request_digest`. It enforces **two** uniqueness keys: `job_id → request_digest`
  (a reused id must carry the same digest) **and** `request_digest` itself.
- **Pre-launch intent:** before calling `systemd-run`, the box **atomically**
  writes an `INTENT{request_digest, id, unit, queue_commit}` record
  (`open(O_CREAT|O_EXCL)` → `write` → `fsync` → `fsync(dir)`); the create-exclusive
  is the launch lock. The **systemd unit name `cmpjob-<id>`** is the second lock: a
  start against a live/known unit is refused by systemd.
**Guarantee: at-most-once launch (honest).** Under `systemd-run --collect`, a unit
that starts and exits quickly can be *collected* before the ledger advances,
making "crashed before start" and "ran once and disappeared" observably identical
(INTENT present, no unit). We do **not** guess between them. Reconciliation (on
restart, before any launch):
  - INTENT present, unit **active** → adopt, do **not** relaunch.
  - terminal already in ledger → replay rejected.
  - INTENT present, **no unit, no terminal** (incl. fast-exit-then-collected) →
    publish terminal **`state: indeterminate, reason: launch_outcome_unknown`**;
    **never auto-relaunch**. Recovery is a *new* request (new `id`) or manual
    reconciliation. **Slicer's own durable resume carries recovery** — a
    resubmitted job resumes its checkpoint and recomputes nothing, so at-most-once
    at this layer + idempotent resume at Slicer = no duplicated work and no lost
    work.
  - anything else → halt `LEDGER-INCONSISTENT`.

The ledger transition table admits exactly: `INTENT → {active-adopt, terminal,
indeterminate}` and `terminal → replay-rejected`. `indeterminate` is a terminal
ledger state for that request digest.

Exactly-once (never `launch_outcome_unknown`) is a later upgrade: drop `--collect`
and retain the unit until the terminal is durably written, so "no unit" truly
means "no start." Not needed for v0 given Slicer resume. **AC4 injects the
fast-exit-and-collected case**, not only active-unit crashes.

---

## 4. Execution

### 4.1 One container

```
systemd-run --unit=cmpjob-<id> --collect --uid=cmpjob
  --property=WorkingDirectory=<registry.workdir>
  --property=MemoryMax=<memory> --property=MemorySwapMax=0
  --property=CPUQuota=<cpu>      --property=RuntimeMaxSec=<registry.timeout_cap §4.3>
  --property=NoNewPrivileges=yes --property=CapabilityBoundingSet=
  --property=ProtectSystem=strict --property=PrivateTmp=yes
  --property=ReadOnlyPaths=<input_roots> --property=ReadWritePaths=<output_roots>
  --property=IPAddressDeny=any             # unless registry network != none
  -- <registry.exec, sha-verified> @<request object, fd-passed §4.2>
```
cgroup limits (runaway dies in its own cgroup), survives its parent, journald
capture, `systemctl show` authoritative, and a **true per-job `memory.peak`** the
poller records into terminal status (the delegated per-stage peak Slicer Sub A
deferred to Sub B — supplied *once this layer is built and proven*, §9).

### 4.2 Identity + hardened root launcher (Pi #4)

- **Jobs run as `cmpjob`** — no sudo/keys/git credential; never `sigma`.
- **Unprivileged poller** parses attacker-controlled queue bytes and schedules;
  a **minimal root-owned launcher** performs the privileged start and **independently
  re-establishes signed-queue provenance** — it does not trust the poller's
  signature decision, path, or supplied bytes (Pi #3). The launcher is invoked
  only as:
  ```
  cmp-launch <registry-key> <queue-commit-sha> <request-id>
  ```
  and then, from a **fixed root-owned bare mirror** of the repo (no poller-selected
  path):
  1. reads `requests/<id>.json` **by commit/blob identity** at `<queue-commit-sha>`,
  2. **verifies the queue commit's signature** against the root-owned
     `allowed_signers` (a parser-compromised poller cannot forge this),
  3. computes the exact-byte `request_digest` and validates against
     `cmp.job-request.v1`, checks `id` grammar and that the commit adds exactly this
     one request (§3.6),
  4. re-resolves **registry key+version**, reloads policy, re-verifies
     **`exec_sha256`**,
  5. launches under the systemd template (§4.1).
  It accepts **no poller-selected filesystem path and no separately-supplied
  request bytes/digest** — rehashing bytes handed over by the same compromised
  poller is not an authorization check. (The earlier "poller supplies a root-owned
  0400 spool object" is removed as internally inconsistent.)
- **Invocation authority is narrow and explicit:** one systemd/Polkit action or one
  exact `sudoers` entry permitting only `cmp-launch <registry-key> <queue-commit-sha>
  <request-id>` — not arbitrary argv, scrubbed environment (only registry-declared
  vars).
- Status is written by the poller, never the job (A10).

### 4.3 Timeout — the box enforces only the registry ceiling (Pi #5)

The admission layer sets **`RuntimeMaxSec = registry.timeout_cap`** and nothing
more. **Workload projection and pre-launch capacity refusal stay inside Slicer**
(its L1 measure-before-scale), which owns the corpus/probe binding. This avoids
the circularity Pi identified — a probe receipt cannot bind to a `request_digest`
that would in turn have to contain the receipt — and keeps a clean authority
split: the box owns the *ceiling*, Slicer owns the *workload judgement*. The box
never accepts a self-asserted projection from a request; `timeout_capacity_refused`
is a Slicer-internal outcome, surfaced (if at all) as a job that Slicer refuses
before doing expensive work. The unit still dies at the registry cap if a job
wedges.

### 4.4 Trigger

`systemd` timer, 60 s, polling `jobs/queue`. Not GitHub cron (drift/drops), not
push/webhook (inbound channel). Bounded, observable staleness.

### 4.5 `jobs/runner` — mutable snapshot, not a log (Pi #2)

An append-only 60 s heartbeat is ~525,600 commits/year while idle — telemetry
masquerading as an event log. `jobs/runner` is **current-state only**: a
**single-writer mutable ref force-updated in place** to a one-entry tree
`runner.json = { seq, observed_at, current_job|null, health, last_error }`, **no
retained per-minute history**. Each snapshot commit is **box-key-signed**, and the
ref stays **CI-inert** (AC10). `jobs/queue` and `jobs/status` remain append-only;
only this telemetry ref is mutable. Poller fetch/API/ref failures set `health` to
`FETCH-FAIL`/`API-BLIND`/`REF-GONE` rather than reading as "no work."
**Anti-rollback (A16):** because force-update is allowed here and a signature
authenticates an old snapshot, runner readers pin the highest accepted `seq`; a
signed snapshot with a **lower/equal non-idempotent `seq` is `RUNNER-REPLAY`**, and
a regressed `observed_at` is **stale, never healthy**.

---

## 5. Invariants (cannot be expressed)

| # | invariant | enforced by |
|---|---|---|
| A1 | a request cannot name an executable | `job` is a registry key |
| A2 | what the box runs cannot change via git | box-local root-owned registry; `exec_sha256` |
| A3 | a job cannot exceed its budget | registry cgroup/systemd properties |
| A4 | a job cannot escalate | `cmpjob`; NoNewPrivileges; empty caps |
| A5 | a job launches at most once | atomic ledger (2 keys) + unit-name lock; ambiguous ⇒ never auto-relaunch (§3.5) |
| A6 | a job cannot be orphaned | transient unit, independent lifetime |
| A7 | a job cannot declare its own success | terminal = systemd exit, poller-published |
| A8 | stalled ≠ running | monotonic progress counter + timestamp |
| A9 | dead poller ≠ idle box | `jobs/runner` snapshot + health codes |
| A10 | status cannot be forged by the job | poller writes; job has no git credential |
| A11 | no operational ref triggers a workflow | runtime CI-inert check on all three |
| A12 | root launcher never trusts poller input | independent re-resolve/verify; fd-only; TOCTOU-safe |
| A13 | no unsigned/unauthorized request is admitted, no forged status is trusted | signature check vs box-local `allowed_signers`; box-key-signed status (§3.1) |
| A14 | request identity is reproducible | frozen wire bytes + exact-byte digest + linear cursor (§3.6) |
| A15 | queue tamper/rewrite cannot pass unnoticed | pinned `last_accepted_commit`; non-descendant ⇒ `QUEUE-TAMPER` (§3.1) |
| A16 | a validly-signed *old* status/runner object cannot replay as fresh | pinned status commit+`event_id` (`STATUS-TAMPER`); pinned runner `seq` (`RUNNER-REPLAY`) |

---

## 6. Acceptance criteria (file + check + pass/fail)

| # | covers | pass iff |
|---|---|---|
| AC1 | A1,A2 | `job:/bin/sh`, unknown `job`, or a colon/oversized id → `rejected`; no unit created |
| AC2 | A3 | limits set from registry; induced OOM is `CONSTRAINT_MEMCG`; poller alive |
| AC3 | A4,A12 | uid=`cmpjob`; sudo denied; no key; git-write denied; launcher invoked only as `cmp-launch <key> <queue-sha> <id>` |
| AC4 | A5 | faults injected **before** start, **after start (active)**, and **fast-exit-then-collected**: reconciliation relaunches none; `launch_outcome_unknown` on the ambiguous case; 0 recompute on Slicer resubmit; replay rejected |
| AC5 | A6 | kill poller mid-job → unit stays `active`; poller reconciles terminal from unit exit |
| AC6 | A7,A10 | job exits nonzero and *tries* to self-publish success → poller publishes `failed`; self-success ignored |
| AC7 | A8 | `SIGSTOP` > 2× interval → progress counter stops; `jobs/runner` stays fresh; consumer flags `stalled` |
| AC8 | A9 | stop poller → `jobs/runner` stale/`FETCH-FAIL`; dead-poller ≠ idle-box |
| AC9 | §4.1,§4.3 | terminal records per-job `memory.peak` + `RuntimeMaxSec` |
| AC10 | A11 | push each of the 3 refs → **runtime Actions-API check**: 0 runs triggered |
| AC11 | A2 | duplicate `id`, different request digest → `rejected` (integrity), not replay |
| AC12 | A13 | a queue commit that is unsigned or signed by a non-`allowed_signers` key → `QUEUE-UNSIGNED`, never admitted; a `jobs/status` commit not signed by the box key → not trusted by δ's verifier |
| AC13 | A14 | two independent processes derive the same `request_digest` and cursor position from `jobs/queue`; a merge/multi-add/mutating commit is rejected `QUEUE-NONLINEAR`/malformed |
| AC14 | A12 | **compromised-poller simulation:** a schema-valid request whose queue commit is *unsigned or wrong-key* cannot be launched — the launcher's independent commit-signature check rejects it |
| AC15 | A15 | force-push/rewrite that drops `last_accepted_commit` from queue history → admission halts `QUEUE-TAMPER` (falsifiable), not silent |
| AC16 | A16 | replay of an earlier **genuinely box-signed** status commit / runner snapshot is detected (`STATUS-TAMPER`/`RUNNER-REPLAY`), not accepted on signature validity alone |
| AC17 | §3.5 | the `indeterminate`/`launch_outcome_unknown` terminal is emitted on the fast-exit-then-collected case and is never auto-relaunched |

---

## 7. Deliberate non-goals

No retries, no DAG, no dynamic job registration, no log shipping, no
priorities/fairness/parallelism (one at a time, FIFO), no auto-escalation to an
agent.

---

## 8. Migration

1. Build alongside the workflow (no cutover).
2. Register `slicer` (versioned impl + `exec_sha256`).
3. **Prove AC1–AC17**, then dispatch one job **both ways** and compare receipts.
4. Switch the dispatcher to `jobs/queue` (signing with the `dispatcher` key in the
   box-local `allowed_signers`).
5. **Unregister the self-hosted runner** — deletes the class.
6. Retire `slicer-status/**` for the `jobs/status` stream.

Rollback before step 5 = re-enable the workflow trigger.

---

## 9. Decisions

- **Transport trust (operator-selected: B).** Git-native signed objects: queue
  commits SSH-signed by a `dispatcher` key, status/runner by the box key, both
  verified against a **box-local root-owned `allowed_signers`**. No GitHub org,
  App, or ruleset — `cmp` is a User repo and, more to the point, the box should
  trust signatures it verifies, not GitHub's ACLs. Ref write-access is not the
  boundary; signature verification is.
- **Queue-ref repo:** `usurobor/cmp` for v0; extract only for a second execution
  host/security domain or a non-CMP consumer.
- **Poller code home:** `cmp` until a second consumer appears.
- **Adoption gate:** this design cannot discharge Slicer's implementation ACs. The
  admission layer must be **built and proven through AC1–AC17** before it supplies
  per-job cgroup evidence to Sub B. Implementation-cell dispatch and cutover are
  **not** authorized by this document.

---

## 10. Cost

A poller (~150 lines) + a minimal root launcher, a registry schema, a status
writer + status/queue/runner ref-writers, an atomic admission ledger, a
box-local `allowed_signers` + signature verification, one new user, two signing
keys (dispatcher, box). No broker, no daemon, no database, no GitHub org. If the
implementation much exceeds ~300 lines, re-review rather than push through.
