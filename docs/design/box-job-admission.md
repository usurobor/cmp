# Box job runner — design (minimal)

**Status:** `v0.6.0` — **deliberate simplification.** v0.2→v0.5.1 over-engineered a
single-dispatcher, single-job, ~1-job/hour, private-repo box into a hardened
multi-writer admission service (1023 lines vs its own ~300 guard). Operator flagged
KISS/YAGNI; retro: `threads/rca/2026-08-04-runner-overengineering.md`. This version
keeps only what the original RCA needs and states its threat model up front so the
scope cannot silently re-inflate.

**Design:** ω (Omega) + δ (cn-sigma@cmp:claude/chat). **Applies:** `process-economics`
(the lightest thing that earns its cost), core `design`, `performance-reliability`.

---

## 1. Threat model + non-goals (stated first, on purpose)

**Actors:** one dispatcher (**δ**), one box (**cn-sigma**), one job type
(**`slicer`**), a **private** repo. ~1 job/hour.

**Defended (real, from the 2026-08-01 RCA):**
- a job cannot take down the box (global OOM), orphan processes, or run as root;
- the box runs no arbitrary command — only a **registered** job;
- GitHub does not decide what executes on the box (no inbound execution channel);
- a job's work is not silently lost or duplicated across aborted runs.

**Out of scope for v0 (explicit — a "defect" against these is not a defect):**
- a **compromised on-box poller** (if the box is owned, in-process boundaries do
  not save you; single tenant);
- **git-ref forgery / rollback** (private repo, single δ writer with a normal
  credential; the box-local registry already bounds what a rogue queue entry can
  do — request `slicer`, nothing else);
- multi-tenant / multi-dispatcher fairness, DAGs, retries, dynamic registration.

If any of these becomes real (a second dispatcher, a public repo, mutually-
distrusting tenants), **re-open the threat model first**, then add the specific
mechanism. Not before.

---

## 2. Design — five load-bearing pieces

1. **Pull, don't push.** A `systemd` timer (60 s) runs an unprivileged poller that
   `git fetch`es a queue ref **δ** writes (`refs/heads/jobs/queue`, append-only,
   one `requests/<id>.json` per commit). Nothing outside can cause execution; the
   self-hosted GitHub Actions runner can be unregistered.

2. **Box-local registry — the ref names a job, never a command.**
   `/etc/cmp-jobs/registry.yaml`, root-owned, **not in any repo**. A request is
   `{id, job, params}`; `job` is a registry key. Changing what the box will run
   needs box access, not a git push. Unknown job / bad params → rejected to status.
   ```yaml
   jobs:
     slicer: { exec: /opt/cmp/bin/slicer-run, user: cmpjob,
               memory: 10G, cpu: 600%, timeout: 6h,
               params: { mode: [sample, full], n: {type: int, max: 400000} } }
   ```

3. **Sandboxed unit — the actual safety.** Each job runs as:
   ```
   systemd-run --unit=cmpjob-<id> --collect --uid=cmpjob
     --property=MemoryMax=<memory> --property=MemorySwapMax=0
     --property=CPUQuota=<cpu>     --property=RuntimeMaxSec=<timeout>
     --property=IPAddressDeny=any  -- <exec> <validated params file>
   ```
   cgroup-bounded (a runaway dies in its own cgroup, not the box), time-limited,
   **non-root** `cmpjob` (no sudo/keys/git-cred), network-off. This is the security
   boundary: registry + sandbox. `systemctl show` is the authoritative run state;
   journald captures output; per-job `memory.peak` is recorded.

4. **Observable — silence is falsifiable.** The poller force-updates a small
   snapshot ref `refs/heads/jobs/status` = `{ id, state, seq, observed_at,
   memory_peak, current_job|null, health }` (mutable, single-writer, no history).
   Terminal state is the systemd unit exit, published by the poller (a job cannot
   declare its own success). A stale snapshot ⇒ box down; fresh with
   `current_job:null` ⇒ idle; `health: FETCH-FAIL` ⇒ can't reach origin. Any reader
   (δ) verifies liveness by fetching, no privilege needed.

5. **At-most-once + Slicer's own resume.** Before launch the poller writes a durable
   `launched/<id>` marker (`O_EXCL`); if it exists, don't relaunch. One job at a
   time (launch only if no `cmpjob-*` unit is active). Ambiguous outcomes are never
   auto-relaunched — **Slicer already checkpoints and resumes**, so a re-submitted
   job resumes and recomputes nothing. The runner does not re-solve exactly-once;
   it borrows the guarantee from the layer that already has it (Slicer L2).

---

## 3. What was cut, and why (YAGNI)

Removed vs v0.5.1, because the §1 threat model does not need them: git-native
**signed-object transport**, **anti-rollback** (STATUS-TAMPER/RUNNER-REPLAY), the
**privileged-boundary verb API** (`cmp_admitd`), the **SQLite transactional
ledger/outbox**, **remote-ack verification**, **FIFO-in-privileged-state**, and all
**compromised-poller** hardening. Each was a correct answer to an out-of-scope
adversary. The registry + sandbox carry the real security; a durable marker + a
single-active check carry the real idempotency.

---

## 4. Acceptance criteria (small, real)

| # | check | pass iff |
|---|---|---|
| R1 | queue `job:/bin/sh` or unknown job / bad params | rejected to status; **no** unit created |
| R2 | queue `slicer` at `n` max, induce over-budget | `MemoryMax` set; OOM is `CONSTRAINT_MEMCG`; **poller + box survive** |
| R3 | inspect a running job | uid=`cmpjob`; no sudo/key/git-cred; network denied |
| R4 | kill the poller mid-job | unit stays `active`; poller reconciles terminal from `systemctl show` on restart; marker prevents relaunch |
| R5 | re-submit a completed `id` | marker ⇒ no relaunch; Slicer resume recomputes 0 |
| R6 | stop the poller | `jobs/status` goes stale / `FETCH-FAIL`; reader distinguishes dead-box from idle |
| R7 | push to `jobs/queue` / `jobs/status` | runtime Actions-API check: **0** workflows triggered |

R2–R6 need real systemd/cgroups → proven at **isolated box staging** (fixture
identity, test refs, temp `cmpjob-staging`) — an operator step. R1/R7 are provable
at fixture level.

---

## 5. Migration & gates

1. Build the ~100-line poller + registry against R1/R7 (fixtures).
2. **Operator-authorized isolated staging** on cn-sigma (temp `cmpjob-staging`,
   test refs, harmless fixture exec) proves R2–R6 on real systemd/cgroups.
3. Register `slicer`; dispatch one job both via the old workflow and the queue ref;
   compare receipts.
4. Switch δ dispatch to `jobs/queue`; **unregister the self-hosted runner** (deletes
   the inbound channel).

Production `cmpjob`/registry install, live dispatch, runner unregister, and cutover
remain operator decisions. Slicer Sub B is unrelated and unauthorized here.

---

## 6. Cost

A poller + registry + status writer + a systemd timer + one `cmpjob` user. Target
**~100 lines**. If it materially exceeds that, re-open §1 (the threat model grew) —
do not add mechanism silently.
