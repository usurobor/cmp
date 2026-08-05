# Beta Review — issue #5 / PR #4

**Verdict:** REQUEST CHANGES

**Round:** 1
**Reviewed code SHA:** `2dafd8c20693fe90fd1e523e1a5ceff42f59b4bc`
**Fixed this round:** n/a
**Branch CI state:** provisional — no check suite exists for the reviewed SHA; latest listed branch run predates the implementation
**Merge instruction:** none — do not merge or deploy until all findings clear in a new exact-SHA review
**Candidate branch:** `claude/cmp-experiment-001-issue-c1oov4`

## §2.0.0 Contract Integrity

| Check | Result | Notes |
|---|---|---|
| Status truth preserved | no | The design promises remotely visible progress and authoritative terminal state; the implementation publishes neither progress nor the local receipt. |
| Canonical sources/paths verified | no | `docs/design/box-job-admission.md` specifies `{id, job, params}` and a nested registry; issue #5 and `runner/v0` specify `{id, mode, n}` and a flat registry. |
| Scope/non-goals consistent | no | `mode: full` is admitted although full-corpus work is a non-goal; the PR also contains six legacy workflow files and unrelated experiment history. |
| Constraint strata consistent | no | The issue's hard stop at a materially exceeded ~100-line poller was not reopened with the operator; the reviewed poller is 143 nonblank/noncomment lines (176 with the fixed entrypoint). |
| Exceptions field-specific/reasoned | n/a | No protocol exemption is recorded. |
| Path resolution base explicit | yes | Runtime install paths and the mirror/state roots are named in `runner/v0/DEPLOY.md` and the example config. |
| Proof shape adequate | no | AC4 requires two remote progress snapshots, but the poller never includes progress in the status tree and the proposed small line scan can finish before two timer ticks. |
| Cross-surface projections updated | no | Design, issue, README, implementation, PR body, and tests do not describe one coherent request/status schema. |
| No witness theater / false closure | no | AC2/AC8 fixtures pass, but the effect shell carrying fetch/publish/launch is untested and contains deterministic failures. |
| PR body matches branch files | no | The PR body describes a design/RCA track; the branch now includes the v0 runner and deploy runbook plus unrelated experiment artifacts. |
| γ artifacts present (gamma-scaffold.md) | no | `.cdd/unreleased/5/gamma-scaffold.md` is absent; issue #5 has neither a §3.11b protocol exemption nor a discoverable wave manifest. |

## Contract-pass findings

| # | Finding | Evidence | Severity | Type |
|---|---|---|---|---|
| D1 | Restore the required γ artifact before implementation review can pass. | No `.cdd/` tree exists at reviewed SHA; issue #5 contains no protocol exemption or wave link. | D | protocol-compliance |
| D2 | Reconcile the canonical request, registry, status, progress, memory, and resume contracts across design, issue, README, code, and tests. | `docs/design/box-job-admission.md:47-55,68-82` conflicts with `runner/v0/poller.py:32-45`, `runner/v0/README.md:20-57`, and issue #5. | D | contract-integrity |
| D3 | Recut this as a runner-only branch from current `main`; do not merge the six retired workflow files or unrelated experiment/data history. | `git diff origin/main...2dafd8c` includes six `.github/workflows/*.yml` files and 57 additional files outside `runner/v0`. | D | scope-integrity |
| C1 | Reopen or satisfy the explicit process-economics stop gate. | Issue #5 says materially exceeding ~100 lines is a STOP; reviewed implementation is 176 nonblank/noncomment Python lines, while README calls the overage irreducible without operator acceptance. | C | process-economics |
| B1 | Add a CI/check result for the exact implementation SHA. | PR `statusCheckRollup` is empty; the newest branch run predates the runner implementation. | B | verification |

## Regressions Required (D-level only)

- D1 positive: a valid `gamma-scaffold.md` or auditable exemption/wave artifact is discoverable; negative: absence remains a protocol blocker.
- D2 positive: one fixture validates the same schema and status projection documented on every canonical surface; negative: stale `{id, job, params}` / `seq` / `memory_peak` claims fail a consistency check.
- D3 positive: `git diff origin/main...HEAD --name-only` contains only issue #5 runner, documentation, tests, and CDD artifacts; negative: retired workflows and unrelated experiment artifacts are absent.

## Notes

Phase 1 completed before implementation review, per `cnos.cdd/cdd/review`.

## §2.1 Implementation Review

### §2.0 Issue Contract

#### AC Coverage

| # | AC | In diff? | Status | Notes |
|---|---|---|---|---|
| AC1 | Poll ≤60 s and launch fixed entrypoint | yes | blocked | Timer exists, but launch failure is poisoned by a durable marker and cannot recover. |
| AC2 | Reject invalid requests without a unit | yes | fixture-pass | `python3 runner/v0/tests/test_ac2_ac8.py` passes on the reviewed SHA. |
| AC3 | Real unit limits, non-root, no network/keys | partial | unproven | argv is present; the on-box proof is correctly deferred, but filesystem ownership prevents the job from completing. |
| AC4 | Remotely visible advancing progress | no | fail | Local progress is never copied into the Git status snapshot. |
| AC5 | Unit-derived authoritative terminal state | partial | fail | `activating` is called failed; collected/missing units fall back to a permanent `launched` marker state. |
| AC6 | Duplicate suppression and Slicer resume | partial | fail | Duplicate suppression exists; the fixed entrypoint has no resume/checkpoint behavior and a failed launch is never retryable. |
| AC7 | Poller restart adopts running unit | partial | fail | Exact `active` units are adopted; transitional and already-collected states are not truthfully reconciled. |
| AC8 | Job refs trigger zero workflows | yes | fixture-pass | Current workflow branch filters are inert for `jobs/*`; fixture passes, though no current-head CI run exists. |

#### Named Doc Updates

| Doc / File | In diff? | Status | Notes |
|---|---|---|---|
| `docs/design/box-job-admission.md` | yes | inconsistent | Retains an incompatible request/registry/status/resume contract. |
| `runner/v0/README.md` | yes | inaccurate | Claims observable progress/resume and documents a bare mirror that `publish()` cannot use. |
| `runner/v0/DEPLOY.md` | yes | unsafe | State ownership prevents `cmpjob` from writing progress/receipt. |
| PR body | n/a | stale | Does not describe issue #5's implementation or the actual branch scope. |

#### CDD Artifact Contract

| Artifact | Required? | Present? | Notes |
|---|---|---|---|
| `.cdd/unreleased/5/gamma-scaffold.md` | yes | no | Mandatory D blocker; no exemption/wave substitute. |
| `.cdd/unreleased/5/beta-review.md` | yes | yes | This review, written incrementally by phase. |

#### Active Skill Consistency

| Skill | Required by | Loaded? | Applied? | Notes |
|---|---|---|---|---|
| `write-functional` | issue #5 | yes | partial | Pure seams exist, but effect failures are discarded and state transition ordering is unsafe. |
| `performance-reliability` | issue #5 | yes | no | Transitional/vanished unit recovery and observable progress are incomplete. |
| `process-economics` | issue #5 | yes | no | The explicit line-budget STOP was bypassed rather than reopened. |
| `cdd/review` | issue #5 / operator | yes | yes | Contract-first review, exact-SHA evidence, regression pairs, single verdict. |

### Implementation findings

| # | Finding | Evidence | Severity | Type |
|---|---|---|---|---|
| D4 | Make status publication work with the documented bare mirror and fail closed on every Git plumbing/push error. | `publish()` sets `GIT_INDEX_FILE=<gitdir>/.git/index.status` and ignores all subprocess return codes (`poller.py:152-162`); `git clone --mirror`/`git init --bare` in `DEPLOY.md:37` and `README.md:65-66` has no `.git/`. Reproduction returned successfully while creating no index parent, local ref, or remote ref. | D | correctness |
| D5 | Make launch admission transactional so a failed params write or `systemd-run` does not create a permanent false `launched` state. | Marker is created before `write_params()` and `launch()` (`poller.py:79-87`). A throwing launcher leaves the marker, writes no status, and the next poll reports `state=launched, reason=marker` without launching. | D | reliability |
| D6 | Establish least-privilege writable directories for `cmpjob` progress/receipt while keeping poller markers/status protected. | Deploy creates `/var/lib/cmp/runner` owned by `cmppoll` (`DEPLOY.md:36`); the params point `cmpjob` below that tree (`poller.py:102-108`), and `atomic_write()` creates/writes those directories (`slicer_run.py:15-20`). A different-user reproduction fails with `PermissionError`. | D | security-correctness |
| D7 | Publish actual progress (and a trustworthy terminal receipt projection) to `jobs/status`. | Entrypoint writes only local files (`slicer_run.py:30-41`), while `poll_once()` publishes only dispatch statuses and `runner.json` (`poller.py:170-175`). AC4 cannot pass. | D | observability |
| D8 | Reconcile the complete systemd state machine and preserve terminal truth after `--collect`. | `observe()` recognizes only `ActiveState=active` (`poller.py:48-55`): `activating` reproduces as `failed`. If a collected unit disappears, the marker path reports `launched` forever and no terminal truth is recoverable. | D | reliability |
| D9 | Make bounded-sample completion and resume claims honest. | `slicer_run.run()` returns success when EOF yields fewer than `n` rows (`slicer_run.py:23-34`); the entrypoint has no checkpoint/resume input, contrary to design `:78-83` and README `:73-75`. | D | honest-claim |
| C2 | Reject `mode: full` until that behavior is designed and tested. | `registry.yaml` admits `sample,full`, `validate()` accepts it, and the entrypoint ignores mode; issue #6 explicitly excludes full mode. | C | scope |

### Reproduced behavior

On exact code SHA `2dafd8c20693fe90fd1e523e1a5ceff42f59b4bc`:

- Official fixture: AC2 reject PASS; AC2 valid PASS; AC8 PASS (6 workflows).
- Bare-mirror publish: returned without error; local status ref absent; remote status ref absent; index parent absent.
- Launch failure: durable marker present; status absent; retry launch count `0`; retry falsely reports `state=launched`.
- Split-user write: `cmpjob`-equivalent writer receives `PermissionError` below the `cmppoll`-owned state root.
- `observe(ActiveState=activating)`: returned `state=failed`.
- Short dataset: requested `10`, read `1`, returned a successful receipt rather than failure/partial.
- `mode=full`: admitted as valid.

### Regressions Required (D-level implementation findings)

- D4 positive: a temporary bare mirror publishes the exact expected status tree locally and to a test remote; negative: invalid index, commit, ref update, or push returns nonzero/raises and never reports a successful heartbeat.
- D5 positive: successful launch creates one durable admission record and a duplicate cannot relaunch; negative: params/launch failure yields an explicit failed/retryable state without a false marker or false `launched` report.
- D6 positive: the real `cmpjob` user can atomically write only its assigned progress/receipt paths; negative: it cannot mutate poller-owned launch markers, statuses, config, or mirror refs.
- D7 positive: two distinct local progress generations become two fetchable `jobs/status` snapshots; negative: absent/stale/malformed progress is explicitly represented and never fabricated.
- D8 positive: `activating`, `active`, successful, failed, timed-out, OOM, and restart/adoption paths map to truthful states; negative: missing/collected/unknown unit state cannot become false success or permanent nonterminal `launched`.
- D9 positive: exactly `n` rows produce a deterministic receipt and a documented checkpoint can resume without recomputation if resume remains an AC; negative: premature EOF or unusable checkpoint fails explicitly.

## Implementation notes

The two fixture-level tests and `py_compile` pass, but they do not exercise the effect shell where the deploy blockers occur. No deployment is authorized by this review state.

## §3 Verdict

**REQUEST CHANGES.** The implementation is not deployable. The documented bare-mirror path cannot publish status, failed launches are recorded permanently as launched, the split service users cannot write the job artifacts, and progress never reaches the remote status ref. Contract/schema drift, missing CDD scaffold, absent current-head CI, and the branch's unrelated legacy workflows independently prevent merge.

Required repair order:

1. Recut a clean issue-#5 branch from current `main`, preserving the runner work but excluding all unrelated experiment history and retired workflows.
2. Add the γ scaffold and reconcile the design/issue/README/test schemas before changing implementation.
3. Fix publication error handling, launch transaction/recovery, split-user filesystem ownership, remote progress projection, and systemd terminal reconciliation with the regression pairs above.
4. Remove unsupported `full` and resume claims or implement and prove the exact agreed behavior; honor the line-budget STOP explicitly.
5. Run fixture and effect-shell integration tests in CI on the exact candidate SHA, then request a fresh beta review. Only an `APPROVED` round may authorize issue #6 deployment.

No code from this branch was deployed.
