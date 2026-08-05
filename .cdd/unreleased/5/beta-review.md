# Beta Review — issue #5 / PR #4

**Review state:** IN PROGRESS — contract pass complete

**Round:** 1
**Reviewed code SHA:** `2dafd8c20693fe90fd1e523e1a5ceff42f59b4bc`
**Fixed this round:** n/a
**Branch CI state:** provisional — no check suite exists for the reviewed SHA
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
