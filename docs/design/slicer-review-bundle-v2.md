# CMP Slicer — Review Bundle v2

**Bundle version:** v2  (Part 3 = Slicer issue **v2.1**, post 2nd review)
**Assembled:** 2026-08-03T00:57:14Z

## Provenance (disambiguated)
```yaml
source_artifacts_sha: 47766edb3d3d443357de42b7ccfe3773f200bf93   # the commit these three parts are taken from (authoritative)
bundle_commit_sha:    (this file; committed immediately after — see git log for docs/design/slicer-review-bundle-v2.md)
repo:                 usurobor/cmp
branch:               claude/cmp-experiment-001-issue-c1oov4
```
The prior bundle header conflated the assembled-at SHA with the commit SHA; this
header separates them. All three parts below are the exact bytes at
`source_artifacts_sha` (47766ed).

## What this is
A single self-contained artifact for external review — no repo access needed. It
carries the full chain in dependency order.

| Part | Artifact | Role | Version |
|---|---|---|---|
| 1 | RCA — 2026-08-01 embed memory blowup | why the design is shaped this way | as committed |
| 2 | Design — compute pipeline architecture | invariants + rationale (executables cede to Part 3) | + authority note |
| 3 | Slicer issue | the dispatchable executable contract | **v2.1** |

Reading order top-to-bottom: Part 1 names the failure class; Part 2 turns it into
invariants; Part 3 binds it into a testable contract. **On any executable specific,
Part 3 supersedes Part 2.** Status: design only, nothing built (RCA freeze holds).

## Changelog v1 → v2 (issue v2 → v2.1), by the 2nd review's blockers
1. Part 2 vs contract reconciled (authority note both docs).
2. Retrieval preregistered: calibration/held-out control split; instrument-validity
   gates vs measured novel yield.
3. Control recall measured on RAW top-k before baseline subtraction; two artifacts;
   recall defined precisely.
4. Invalid "prove exact non-quadratic" removed; exact only as bounded O(m·N)
   reference; full-corpus exact out of scope.
5. Heartbeat mechanized via runner self-round-trip; refusal_gate generalized.
6. Capacity from cgroup-effective + whole-cgroup peak; branch-sensitive enforcement.
7. Resume generalized to every projected-long stage.
8. Retrieval-input allowlist (blindness before embedding); volume only in eval
   metadata; recursive key validation, not grep.
Plus two status axes (instrument vs discovery) and measured-N (not hardcoded).

---
---

# PART 1 — Incident RCA

_Source: `threads/rca/2026-08-01-embed-memory-blowup.md`_

# RCA: CMP Sub-2 embedding encode — a day lost to unmeasured jobs on a 2 GB box

**Date:** 2026-08-01
**Severity:** Medium (no data loss, no production impact; ~7h of engineering time and box time burned, zero result produced)
**Duration:** ~10:18 first contributing event → ~19:20 halt for RCA (~9h wall; ~7h of it directly on the embedding slice)
**Status:** DIAGNOSING — no remediation until signed off.

## Summary

The Sub-2 embedding-retrieval slice was launched five times over ~7 hours and
never produced a result. The proximate failures were different each time (a
blocked runner, a path PermissionError, a push race, then a memory blowup that
swapped the box), but they share one systemic root: **expensive jobs were
launched on a known 2 GB / 1 vCPU box without first measuring their resource
envelope on a representative sample, and without pre-flight observability — so
each mis-sized job failed slowly and invisibly instead of failing fast with a
number.** The memory blowup was the *third* memory-accumulation incident on this
box the same day (collector OOM, gen_candidate_pairs throttle, embed swap),
which is the signal that the cause is a missing process gate, not any single bug.

## Timeline (UTC)

| # | Time | Event |
|---|------|-------|
| 1 | 10:18 | `gen_candidate_pairs` run `30695440755` starts; on the 2 GB box it reclaim-throttles to ~22% CPU (715k reclaim events, ~1.2 GB held) and never makes progress. |
| 2 | 12:25 | Box watchdog goes blind: systemd unit has no `HOME`, so `gh` returns rc=4 and every remote check is silently skipped (`2>/dev/null` + `continue`). Blind until 17:45. |
| 3 | 12:26 | Embed run #1 (`30699683175`, sha dfd9ea5) created; single self-hosted runner is occupied by the stale gen job, so it sits `queued`. |
| 4 | 12:33 | Stale gen job cancelled; runner frees. |
| 5 | ~12:38–13:06 | Cloud escalates on the channel that the encode is stuck queued; box is unattended (its recorder does not wake it), asks sit unread. |
| 6 | 14:28 | Runner recovers; run #1 picks up and **fails in 2 s**: `os.makedirs('/var/lib/cmp/data/derived/embeddings')` → `PermissionError` (runner can read the box-owned corpus dir but cannot mkdir under it). Failure-marker push is **rejected non-fast-forward** (run pinned to old sha), so the branch never moves and cloud is not woken. |
| 7 | 16:42 | Run #3 (`30708717121`, sha a8df2bc) starts with EMB_DIR moved to the workspace and the push race fixed. Begins encoding. |
| 8 | 16:42–19:12 | Run #3 is **memory-bound and swapping**: RSS 1340 MB, 1028 MB in swap, 470k reclaim events, **37% CPU**. Cloud cannot see this — GitHub's API 404s in-progress logs — and reports "healthy, ~2h" from step-state alone. |
| 9 | 19:03 | Cloud adds a git-ref progress heartbeat (run #4, `30713926108`) to gain visibility; it queues behind run #3. |
| 10 | ~19:06 | Box (re-attached, watchdog fixed) reports from root: run #3 is swapping, not slow; clean-binary subset is **222,855** (not the ~150k modelled); encode is **3.2% done** (7,170 / 222,855 vectors) after 2h20m → **~70 h** projected. |
| 11 | ~19:11 | Cloud kills run #3 and #4; ships fix c49402b (ENC_BATCH 256→16, shorter text, reservoir-sample to 60k). |
| 12 | 19:12 | Run #5 (`30714258514`) starts; heartbeat confirms visibility (`[1] filtering clean-binary`). |
| 13 | ~19:20 | Operator halts all processes to run this RCA. Run #5 cancelled; all watchers stopped. |

## Five Whys

1. **Why did the embedding slice consume ~7 h and produce no result?**
   → Its final run ran out of memory and swapped the box (3.2% in 2h20m), after
   three earlier runs failed on a blocked runner, a path permission, and a push
   race.

2. **Why did it run out of memory?**
   → At `ENC_BATCH=256` with ~450-token texts, onnxruntime's attention tensor
   (batch × heads × seq²) is ~3.2 GB and the arena retains the peak; and the
   working set was mis-sized because the clean-binary subset was **222,855**, not
   the ~150k assumed. Both are numbers, not judgements — and neither was measured.

3. **Why was the memory footprint not known before launching?**
   → No capacity check preceded the run: the subset size, per-item peak RSS, and
   encode throughput were never measured on a small sample before committing the
   full job to the constrained box. The design asserted "streams, well under the
   cap" from an unvalidated mental model.

4. **Why was there no capacity check?**
   → There is no standing step that requires one. The job was authored and
   launched optimistically, the same way the collector (OOM ×2) and
   `gen_candidate_pairs` (throttle) were earlier the same day — three instances of
   the same class in one day.

5. **Why does the same class of failure keep recurring?** → **ROOT CAUSE:**
   There is no capacity-and-observability pre-flight gate for jobs on the shared
   2 GB / 1 vCPU box. Nothing forces a measured resource envelope (peak RSS vs
   cap, dataset size, projected wall-clock) on a representative sample before a
   full run, and nothing guarantees live progress is visible — so mis-sized jobs
   are admitted, then fail slowly and invisibly rather than being rejected up
   front with a number.

## Contributing causes (documented, not blamed)

- **Observability gap (both ends).** GitHub's API 404s in-progress logs; there
  was no job heartbeat; the box watchdog was blind 12:25–17:45 (no `HOME`); the
  box side was a *recorder*, not a *monitor* — it recorded state to a file that
  never woke anyone. Failures therefore took hours to surface.
- **Single runner, no capacity awareness.** One stale throttling job (step 1)
  blocked the entire queue (step 3) because nothing tracks whether the box is
  already saturated before admitting more.
- **Two latent code bugs compounded the diagnosis** (steps 6): a non-runner-
  writable output path, and a push that assumed fast-forward. Each individually
  minor; together they hid the real signal (the branch never moved).
- **`Restart=always` masked, then delayed.** The runner-down window (~12:25–14:28)
  looked like a hang because the auto-restart eventually recovered it — but only
  after a ~2 h queue timeout had already been consumed.

## Anti-pattern check (per skill)

- Not "human error" — the *system* admitted unmeasured jobs. The design-around is
  a gate, not "be more careful."
- Prefer automation — the preventive actions are a pre-flight probe and a
  heartbeat, not "add more review."
- Has concrete, owned action items (below).

## Actions (proposed — pending sign-off; NOT yet implemented)

```yaml
actions:
  - action: "Pre-flight capacity probe: before any full box job, encode/process a
             fixed small sample (e.g. 1–2k), measure peak RSS + throughput, and
             extrapolate to full N. Abort with a number if projected peak > cap
             or projected wall-clock > threshold."
    owner: sigma
    status: pending
    due: 2026-08-02
  - action: "Measure-N-first: any job whose memory scales with dataset size must
             emit the dataset count (e.g. 222,855) as an early, cheap stage before
             the expensive stage; memory model is derived from the measured N, not
             an assumed one."
    owner: sigma
    status: pending
    due: 2026-08-02
  - action: "Shard-and-checkpoint for large encodes/jobs (box rec #1): process in
             ~20k slices, flush each to disk, free between slices, resume from last
             complete shard. Peak RSS becomes a function of shard size, not corpus
             size; job survives cancellation."
    owner: sigma
    status: pending
    due: 2026-08-03
  - action: "Standing observability on every long box job: git-ref progress
             heartbeat (built, c49402b) as a required workflow element; box
             watchdog is now a real monitor with HOME fix (box, done)."
    owner: sigma
    status: pending
    due: 2026-08-02
  - action: "Record the box capacity envelope (2 GB RAM, 1 vCPU, ~7 GB disk, 2 GB
             swap on same spindle, no GPU) as a hard constraint doc in the repo,
             referenced by any job author; jobs that exceed it must trigger the
             cut-subset / shard / bigger-box decision UP FRONT."
    owner: sigma
    status: pending
    due: 2026-08-03
  - action: "Right-sizing decision is the operator's: a job that structurally needs
             more than the box (full 222,855 encode) surfaces the bigger-box vs
             cut-subset vs shard tradeoff before running, not after a failure."
    owner: operator
    status: pending
    due: 2026-08-03
```

## Sign-off

- [ ] Root cause accepted
- [ ] Actions accepted / amended
- [ ] Remediation freeze lifted

_No code changes, re-runs, or box jobs until the three boxes above are checked._

---
---

# PART 2 — Design: CMP compute pipeline (invariants + rationale; executables cede to Part 3)

_Source: `docs/design/compute-pipeline-architecture.md`_

# Slicer — CMP compute pipeline: design research & recommendation

Component name: **Slicer** — the box-side pipeline that takes the corpus + a
*slice spec* (`mode`, `N`, filters) and produces the blinded candidate fixtures.
Stages: **filter → embed → retrieve → emit fixtures.** Classification/verification
is downstream (reasoning, runs in the sandbox), not part of Slicer. Slicer is what
the *cell* builds and runs; its receipts are what *δ* accepts.

Status: **design only, nothing built.** Written under the RCA remediation freeze
(`threads/rca/2026-08-01-embed-memory-blowup.md`). Applies the cnos.eng lenses:
`performance-reliability` (workload→budgets→failure→recovery→operator-visible
state), `write-functional` (pure core, effects at edges), `process-economics`
(lightest thing that earns its cost).

**Decisions resolved (2026-08-01):** orchestration = **DIY ~200-LOC stage runner**
(not DVC — see §4); box **resized** up from 2 GB / 1 vCPU, so out-of-core is
deferred and parallelism is added (see §4, §9).

> **Authority (2026-08-02).** This document owns the **invariants, rationale, and
> framework research**. It does **not** own the executable specifics — retrieval
> algorithm, worker/thread/batch configuration, shard format, memory model. Those
> are bound by the **Slicer issue** (`docs/design/slicer-issue-draft.md`), and
> **where this document and the issue differ on any executable specific, the issue
> supersedes** (notably §§4, 6, 9 below). Concrete numbers here (e.g. `ENC_BATCH`,
> "concurrent shards", `np.memmap`, "chunked cosine") are **illustrative of the
> reasoning at the time of writing**; the issue's probe-selected config, immutable
> `.npy` shards, and declared heuristic ANN are authoritative.

## Invariants (what our own failures crystallized into)

The day's incidents (collector OOM ×2, gen_candidate_pairs throttle, embed swap,
5 h blind watchdog, every run unresumable) reduce to six invariants Slicer must
**enforce**, not hope for. Every design choice below serves one of these.

- **L1 — Measure before you scale.** A self-calibrating gate runs a small sample,
  extrapolates, and *refuses* an infeasible job with a number.
- **L2 — Every long job resumes.** Checkpointed + idempotent; kill ≠ start-from-zero.
- **L3 — Observable without privilege.** Run-state any observer can query with no
  box access and no browser. Corollary (ω's rule): **silence must be falsifiable**
  — a monitor whose failure is indistinguishable from quiet success manufactures
  false confidence and is worse than none; every watcher emits a distinct signal
  on its own failure modes (blind / gone / unreachable), never a silent skip.
- **L4 — Memory bounded by construction.** Peak RSS = f(shard), never f(corpus).
- **L5 — Errors surface.** Explicit `Result`; no `2>/dev/null` that blinds for 5 h.
- **L6 — Failure is cheap.** Degrade / skip / checkpoint, never collapse.

## 0. Framing — CMP is not a backtester

The instinct to look at "quantstrat systems" is right about the *discipline* and
wrong about the *shape*. Every framework we surveyed (Backtrader, VectorBT,
Zipline-reloaded, NautilusTrader, LEAN) simulates a **trading strategy over a
price series**. CMP's compute is not that. It is a **staged research/feature
pipeline**:

```
corpus → clean-binary filter → embed → semantic retrieval → joint-state classify
       → coherence/verify → (later) price overlay → witness
```

So we borrow along two independent axes:

- **From quant *research* discipline** — the *validation* structure: never fit or
  run on the full universe blindly; in-sample first, staged scale-up,
  walk-forward. (This is a research-methodology import, not an engine.)
- **From data-pipeline engineering** — the *execution* structure: idempotent
  checkpointed stages, out-of-core compute, a data catalog, a run ledger.

The backtester engines themselves are the wrong tool and are not recommended.
Their relevant lesson is negative and universal: **vectorized backtesters run on
subsets and event-driven ones subscribe to bounded date-ranges — none of them
load the whole world and hope.** That is exactly the discipline we lacked.

## 1. Property 1 — a real limited/test mode

**What best-in-class does.** Quant research never runs a strategy on the full
history/universe as the first act. The accepted ladder:

1. **Tracer-bullet run** — the *entire* pipeline, end-to-end, on a tiny
   representative slice, to prove it is wired correctly and produces the right
   shape of output. (Engineering term; the quant analog is "prototype on one
   symbol / one month.")
2. **In-sample → out-of-sample → walk-forward** — validate on a bounded window,
   then roll. Never a single full-universe fit.
3. **Capacity probe** — measure peak RSS and throughput on the slice, extrapolate
   to full N, and *decide* whether the full run is even feasible before starting.
4. **Staged scale-up** — N₀ (1–2k) → N₁ (~60k) → full (222,855), each gate
   passing before the next.

VectorBT is explicitly "ideal for large datasets … blazing speed" *because* it is
run on research subsets first; NautilusTrader subscribes to bounded ranges.

**Recommendation for CMP.** First-class `mode = sample(N) | full`, running the
**identical code path** (no separate dev/prod code — the sample run *is* the
correctness proof for the full run). Plus a **self-calibrating capacity-probe
stage** that:
- reads the box's actual resources at runtime (`nproc`, `MemAvailable`) and
  derives its budgets from them — so the probe is correct on *any* box and a
  resize changes what it measures, not a line of code (this is L1 done right, and
  it is why the box resize is a non-event for the code),
- encodes/processes a fixed small sample,
- measures peak RSS and rows/sec,
- extrapolates to full N,
- **refuses to scale** and reports a number if projected peak > derived memory
  budget or projected wall-clock > threshold.

This is the RCA's pre-flight gate made native. The 70-hour, 3.2%-complete run
would have been rejected at the probe in ~60 seconds with "projected 1.3 GB > 1.2
GB cap, projected 70 h > 2 h budget — refuse."

## 2. Property 2 — idempotent, resumable, explicit provenance

**What best-in-class does.** Idempotency in pipelines is *checkpointing*: discrete
states saved throughout execution so a failed run resumes from the last complete
point rather than the start. The survey:

| Tool | Idempotency model | Weight | Fit for a 2 GB box |
|---|---|---|---|
| Airflow | external scheduler + DB | heavy (daemon+DB+web) | no |
| Dagster | software-defined assets, IO managers, materialization records | heavy; "not yet ready for incremental" | no |
| Prefect | task result caching, retries, versioning | medium-heavy (daemon) | marginal |
| Luigi | `Target` = output exists → skip | light, file-based | yes |
| Snakemake | file-DAG, resumes from partial outputs | light, file-based | yes |
| **DVC** | content-addressed cache + `dvc.yaml` stage DAG; GitOps-native | light | **yes — strong** |

DVC stands out: it gives **idempotency + resumability + content-addressed
provenance + Git-native reproducibility** in one lightweight tool, and it aligns
with the git-ref channel and heartbeat we already run. Its `dvc.yaml` names every
stage's `deps` (raw inputs) and `outs` (derived stores) — which *is* Property 2's
"super clear on where raw data comes from and where data is stored."

**Functional lens (`write-functional`).** Each stage is a **pure transform** at
the type level — `inputs (named paths) → outputs (named paths)` — with all I/O at
the edges and no hidden global state. Pure core (filter logic, joint-state
classification, coherence tests) is trivially testable; the effectful shell
(read corpus, write memmap, push run-state) is thin and audited. Errors are
explicit `Result`-style returns, not exceptions swallowed by `2>/dev/null`
(which is precisely how the box watchdog went blind for 5 hours).

**Recommendation.** Model CMP as a **DAG of checkpointed stages** governed by a
**Data Catalog** — a single config that names, for every dataset:
- logical name, physical location, format, schema, and whether it is a *raw
  source* (read-only, external) or a *derived store* (produced by a stage).

Idempotency = a stage is skipped when its outputs exist **and** the digest of its
inputs + parameters is unchanged. Resumability = the heavy stages (encode,
retrieve) checkpoint at **shard** granularity, so a kill/restart resumes from the
last complete shard. This is the property whose absence made every failure cost
the full elapsed time instead of one shard.

Explicit raw-vs-derived split, concretely:
- **Raw (read-only):** `$CMP_DATA_DIR/derived/liquid_volume_ge_10000.jsonl` (the
  357,664-market corpus — despite the name it is our raw input), plus the raw
  Polymarket page dumps.
- **Derived (produced, resumable, content-addressed):** clean-binary subset,
  embeddings shards, retrieval pairs, blinded fixtures, classifier receipts.
  Location is **runner-writable** (`.embcache`/DVC cache) and/or box-persistent —
  never a box-root path the runner cannot `mkdir` (the PermissionError bug).

## 3. Property 3 — progress the observer can always query

**What best-in-class does.** Two poles: heavy orchestrator UIs (Dagster/Prefect
dashboards) versus lightweight **run-state artifacts** (MLflow run records,
checkpoint manifests, `tqdm`). The `performance-reliability` skill states the
principle directly: the operator must be able to read *what is healthy /
saturated / retrying / blocked / which budget was exceeded* from a projection
like `ready.json` / `runtime.json` at any time.

**Recommendation.** A single **run-state JSON** (the "run ledger"), overwritten
atomically each tick, containing:
```
run_id, mode, stage, shard i/N, rows done/total, peak_rss_mb, budget_mb,
started_at, last_heartbeat, status ∈ {running, degraded, failed, done},
resume_point, last_error
```
Written to the persistent data dir **and mirrored to a git ref** (the heartbeat
already built, generalized from a one-line commit subject to the full state
blob). Any external observer — δ, ω, the operator — reads current
state with one `git ls-remote` + fetch, with **no box access and no browser**.
This closes the "couldn't tail" gap structurally, not by asking a human to watch.

## 4. Framework decision: synthetic best-of-breed, not a monolith

Three families, one recommendation.

- **Backtester engines** (Backtrader/VectorBT/Zipline/Nautilus/LEAN): wrong shape
  (strategy simulation, not feature pipeline), heavy, US-equity-centric data
  models. **Reject.** Keep only the *sample-first* discipline.
- **Heavy orchestrators** (Airflow/Dagster/Prefect): right *patterns*
  (idempotent assets, observability) but a daemon + database + web server is more
  than a 2 GB / 1 vCPU box should host, and adds standing ops burden. Dagster is
  itself "not yet ready for incremental computing." **Reject for now** — revisit
  only if CMP outgrows the box.
- **Lightweight, file/Git-native** (DVC / Snakemake / Luigi) + **out-of-core
  compute** (Polars streaming `.collect(streaming=True)`, DuckDB over Parquet,
  Arrow, `np.memmap`): the actual fit.

**Recommendation — compose, don't adopt a monolith:**

1. **Orchestration + idempotency + provenance: DIY ~200-LOC stage runner
   (DECIDED — not DVC).** A minimal runner replicates the essential subset: stage
   table, idempotent skip (output exists + input-and-params digest unchanged),
   shard checkpoints, resume. Why DIY over DVC, given DVC is a real tool that
   solves this:
   - **Zero new dependencies** — the box just bled hours on missing/So-heavy
     deps; the Slicer stack stays minimal.
   - **One source of pipeline truth.** Our substrate is already git-ref-native
     (heartbeat, channel, run-state JSON). A DIY runner emits *into* that
     run-state natively; DVC introduces its own pipeline-state/cache model that
     would compete with it — two authorities for "where is the run."
   - **The DAG is small** (4 near-linear stages); idempotent-skip is ~50 LOC and
     shard checkpointing is ours regardless.
   - **Reversible** (`process-economics` §2.14): the stage bodies are pure
     transforms that do not care what invokes them, so DVC can be swapped in
     later at no cost to the science code.
   - **Sunset criterion:** revisit DVC when Slicer becomes a *wide*
     multi-artifact DAG or we need cross-run lineage / content-addressed caching
     across experiments. Not now.
2. **Out-of-core stages:** Polars streaming / DuckDB for the filter and join
   stages; sharded encode with `np.memmap` for embeddings. **This is the direct
   OOM fix** — peak memory becomes a function of *shard size*, chosen to fit the
   budget, not of corpus size. DuckDB recommends 100 MB–10 GB Parquet, which also
   satisfies the #2-AC3 Parquet landing the assistant flagged.
3. **Observability:** the run-state JSON + git-ref mirror (§3).
4. **Capacity gate:** the probe stage (§1).

"Synthetic" here means: **borrow the patterns — data catalog, content-addressed
idempotent stages, out-of-core execution, run ledger — without the heavyweight
runtime the box cannot host.** That is the `process-economics` answer: the
lightest composition that closes every failure class we actually hit.

## 5. How this closes each RCA failure (the point of the exercise)

| Incident | Design element that prevents recurrence |
|---|---|
| OOM / swap (3× today) | out-of-core stages + shard budget + capacity probe → peak RSS = f(shard), never f(corpus) |
| Couldn't tail progress | run-state JSON mirrored to a git ref, queryable anytime |
| Couldn't interrupt/resume | shard-level checkpoints + idempotent skip → kill/restart resumes |
| Ran huge job hoping | sample-mode tracer + probe gate + staged scale-up; full run refused if projection > budget |
| Blind watchdog / silent errors | explicit `Result` error paths, effects at the edges; no `2>/dev/null` swallow |
| Non-runner-writable path | Data Catalog fixes each store's location; runner-writable by construction |

## 6. Worked example — encode stage under the `performance-reliability` pattern

**Workload:** encode one shard of ~S clean-binary contracts to 384-d vectors.

**Budgets:** shard S chosen so peak RSS ≤ 900 MB (headroom under the 1.2 GB
high-water); `ENC_BATCH ≤ 16`; per-shard wall-clock ≤ ~5 min; total encode
bounded by ⌈N/S⌉ shards, each checkpointed.

**Steady-state cost model:** per shard = 1 model load (amortized/persistent
process) + S/BATCH inference calls + one memmap-region write + one run-state
update. Memory = model (~130 MB) + attention tensor (batch×heads×seq², bounded by
BATCH=16, seq≈175 ⇒ ~24 MB) + shard vectors (S×384×4). File-backed, reclaimable.

**Amplification paths:** ORT arena retains peak activation (the 3.2 GB bug) →
bounded by small BATCH; fastembed internal re-batch to 256 → bounded by passing
`batch_size`.

**Failure modes:** OOM (prevented by probe + shard budget); process kill
(resumes from last shard); model-download failure (retry, then degrade).

**Degradation:** on projected-over-budget, refuse and emit a number; on shard
failure, checkpoint holds and the run is resumable, not restarted.

**Recovery:** last-complete-shard index in run-state is the resume point;
idempotent skip re-enters there.

**Operator-visible evidence:** run-state JSON (`stage=encode, shard 7/38,
rows 60000/222855, peak_rss 780 MB, status running`) on the git ref.

**Known gaps:** encode throughput on this box is not yet measured (that is what
the probe produces); full-N feasibility is a probe output, not an assumption.

## 7. Process economics — does this earn its cost?

- **Failure class prevented:** the exact one that cost ~7 h today (unmeasured job
  → OOM → invisible → unresumable). Recurs without a gate; the RCA shows it
  recurred 3× in one day.
- **Consumers:** δ (dispatches against the contract and reads receipts), the
  operator (reads run-state), the cell (runs against measured budgets).
- **Cost:** one Data Catalog config, one DAG runner (or DVC adoption), one
  run-state emitter, one probe stage. One-time bootstrap; low steady-state.
- **Lighter alternatives rejected:** "be more careful" (the RCA anti-pattern);
  bare heartbeat alone (fixes visibility, not OOM/resume). Heavier alternative
  rejected: Dagster/Prefect (won't fit the box; standing daemon burden).
- **Automation boundary:** the probe/gate is machine-checkable and must be
  automated, not a human "did you check memory?" step.
- **Sunset:** if CMP moves to a bigger box or managed compute, revisit whether a
  real orchestrator (Dagster) now earns its cost.

## 8. What we want to write (outline for the cell's work order — not built)

1. `catalog.yaml` — the Data Catalog: raw sources + derived stores, locations,
   formats, schemas, raw/derived flag.
2. DIY stage-DAG runner (~200 LOC): idempotent skip, shard checkpoints,
   `mode=sample(N)|full`.
3. Stage bodies: filter (stream corpus), encode (sharded memmap, **parallel
   across cores**, BATCH tuned to the resized box), retrieve (chunked cosine,
   index held in RAM — fits now). Out-of-core (Polars/DuckDB) is **deferred** to
   the stages that actually exceed RAM (pairwise, price overlay); see §9.
4. Self-calibrating capacity-probe stage: read box `nproc`/`MemAvailable`,
   measure peak RSS + throughput on a fixed sample, extrapolate, gate the full
   run.
5. Run-state emitter: atomic JSON write + git-ref mirror; pure-core state
   transitions.
6. Receipt schema δ can verify (per stage: inputs digest, outputs, rows,
   peak_rss, wall-clock, status).

## 9. What the box resize changed (and what it didn't)

The box was resized from 2 GB / 1 vCPU **to 8 vCPU / 16 GB** (runner cgroup caps
now High 10 G / Max 12 G; disk 50 GB unchanged — measured envelope confirmed by ω,
2026-08-02). This changes Slicer's *parameters and one tool choice*, not its
*shape*. The invariants (L1–L6) and the whole design above hold at any size — they
pay off regardless of box, and the O(n²) pairwise / time-series stages will need
them.

- **Simplified (the real change): out-of-core drops from mandatory to selective.**
  At N=222,855 the index is 222,855 × 384 × 4 ≈ **342 MB** — comfortably in RAM
  now. So filter/embed/retrieve use plain numpy + memmap; **DuckDB / Polars-
  streaming is NOT built yet.** Reserve that machinery for the stages that
  genuinely exceed RAM later (pairwise ≈ 5×10¹⁰ candidate pairs; the price
  overlay's time series). Less complexity now (`process-economics`: don't pay for
  it until a stage needs it).
- **Added: parallelism.** Pointless on 1 vCPU, now the main speed lever — encode
  fans out across cores (multiprocess over shards, or raised ORT threads);
  retrieval matmul auto-parallelizes via BLAS. ~5–8× wall-clock.
- **Retuned: budgets and knobs (concrete, on the measured box).** `ENC_BATCH`
  16 → **256** (the ~3.2 GB ORT attention arena that swapped the old box now fits
  with headroom, and batch 256 is materially faster per item); encode **shards
  run concurrently** across the 8 cores and retrieval BLAS parallelizes;
  **full 222,855 in one pass is the default run**, the 60k reservoir demotes to a
  sample-mode dev tracer, and **sharding becomes an availability/resume choice,
  not the only way the job finishes.** The probe still derives every budget from
  `nproc`/`MemAvailable` at runtime, so no box specs are hardcoded — it simply
  measures the bigger box.
- **Unchanged (the shape):** idempotent checkpointed stages, the data catalog,
  the probe gate, sample-mode, functional core, the DIY runner. Observability =
  queryable run-state (L3); its **transport is a separate operational surface,
  not a memory post** (telemetry-as-memory is out per cnos#690) — bound to the
  comms model, not hardcoded. And the heavy out-of-core machinery returns for the
  pairwise / price stages.

Net: the resize made Slicer **simpler and faster**, not different in kind. The
discipline — measure, checkpoint, observe, bound, surface, degrade — is what
prevents another lost day, and it is identical regardless of box.

## Provenance

```yaml
derived_from:
  - rank: r1
    path: .cn-sigma/reflections/daily/2026-08-01.md   # (channels/sigma/cloud ref)
    claim: "Slicer design is the coherence-preserving fallout of the 2026-08-01 incident + quant-systems research"
  - rank: r0
    path: threads/rca/2026-08-01-embed-memory-blowup.md
    claim: "root cause = no capacity+observability pre-flight gate for box jobs"
```

## Sources

- Python backtesting landscape (2026): https://python.financial/
- NautilusTrader vs Backtrader vs VectorBT (2026): https://bullalert.ai/blog/best-python-backtest-engines-2026/
- Backtrader vs Nautilus vs VectorBT vs Zipline-reloaded: https://autotradelab.com/blog/backtrader-vs-nautilusttrader-vs-vectorbt-vs-zipline-reloaded
- Prefect — idempotent data pipelines for resilience: https://www.prefect.io/blog/the-importance-of-idempotent-data-pipelines-for-resilience
- Data pipeline tools 2026 (Dagster): https://dagster.io/learn/data-pipeline-tools
- Comparison of data processing frameworks (Kapernikov): https://kapernikov.com/a-comparison-of-data-processing-frameworks/
- Polars LazyFrame larger-than-memory: https://www.jtrive.com/posts/polars-lazyframe/polars-lazyframe.html
- DuckDB vs Polars on massive Parquet: https://www.codecentric.de/en/knowledge-hub/blog/duckdb-vs-polars-performance-and-memory-with-massive-parquet-data

---
---

# PART 3 — Executable contract: Slicer issue v2.1

_Source: `docs/design/slicer-issue-draft.md`_

# Build Slicer v2.1 — CMP candidate-retrieval instrument (spine-first, runner-cell)

**Mode:** design-and-build.
**Source of truth:** `docs/design/compute-pipeline-architecture.md` (invariants +
rationale) + RCA `threads/rca/2026-08-01-embed-memory-blowup.md`. **Authority:**
this issue owns all *executable specifics* (retrieval algorithm, worker/thread/
batch config, shard format, memory model); **where the design doc and this issue
differ on an executable specific, this issue supersedes** (design §§4, 6, 9 are
illustrative).
**Dispatch target:** one ephemeral Slicer run on the cn-sigma self-hosted runner
(the "cell" = the run; filter/embed/retrieve are **stages, not agents**). δ
dispatches; the run emits a receipt; δ accepts/rejects.
**Governing contract:** invariants **L1–L6**; each AC has a file+check+pass/fail
oracle.

> L1 measure-before-scale · L2 resumable (every projected-long stage) · L3
> observable-without-privilege (silence falsifiable) · L4 memory-bounded-by-
> construction · L5 errors-surface · L6 failure-is-cheap (measured).

## What Slicer is — and is not (TSC framing)

α-like observation/filter + **early β-like heuristic candidate search**. It does
not infer joint states, establish relation standing, build the atlas/law, or
detect price incoherence. Output = **candidate pairs**, never *identified
relations* or *tradeable incoherences*.

```yaml
relation_search_claim:
  kind: heuristic
  bounded_surface:            # corpus boundary + filter predicate digest
  embedding_and_index_digest:
  k:
  controls:                   # injected known-positives
```
A run establishes only: *under this frozen boundary and this declared heuristic
method, these candidate pairs surfaced reproducibly.* A negative reads "this
embedding+index+config did not surface it," never "semantic retrieval does not
work." Full CMP sequence: `Slicer → blinded joint-state classification →
counterexample review → SAT/SMT atlas → frozen law → price LP → trade witness.`

## Non-goals
Classifier/verification (downstream) · **exact full-corpus all-query retrieval**
(that IS the excluded O(n²) pairwise) · the pairwise/global stage · price/volume/
outcome exposure to retrieval OR classification · SAT/SMT/LP · DuckDB/Polars
(deferred) · heavyweight orchestrator · persistent box-agent · claiming filesystem
confinement without a sandbox.

## Preregistration & instrument validity (frozen before evaluation)

To prevent tuning-on-the-test-set, retrieval configuration is **preregistered**
via a **calibration/held-out split of the injected controls**:

```
controls  = calibration_controls ⊎ heldout_controls        # disjoint, seeded
1. select embedding + ANN config + k using calibration_controls only
2. freeze config → embedding_and_index_digest (immutable thereafter)
3. run held-out evaluation ONCE against heldout_controls
4. report held-out recall as the instrument-validity result
```
Reusing held-out controls to retune is a contract violation (digest changes ⇒ new
preregistration ⇒ new held-out set).

**Instrument-validity gates** (distinct from the scientific result):
```yaml
computational_recall_gate:      # approx-kNN vs exact on seeded queries, min pass
relation_control_recall_gate:   # heldout known-positive recovery, min pass
novel_relation_yield:           # MEASURED RESULT — never a gate
```
Interpretation: **low `novel_relation_yield` is a valid negative scientific
result; failing `relation_control_recall_gate` means the instrument failed and any
yield is uninterpretable.** `done` requires **both gates pass** (not just outputs+
digests) — see AC-C3.

## Frozen inputs (pinned + digested; recorded in the receipt)
```yaml
corpus_manifest:        {artifact, digest, market_count}      # liquid vol>=$10k
clean_binary_predicate: {version, digest}
embedding:
  model_id: · model_revision: · input_representation: · dimension: · dtype:
  normalization: · truncation_policy:
retrieval:
  algorithm:            # declared ANN (e.g. hnswlib) — heuristic unless exact-reference
  implementation_version: · metric: · k: · index_parameters: · query_parameters:
  seed: · search_claim: heuristic
mechanical_baseline:    {artifact, digest, generation_commit, method_version, pair_canonicalization}
retrieval_input:        {allowed[], forbidden[], rendered_template_digest}   # AC-B0
control_split:          {calibration_ids_digest, heldout_ids_digest, seed}
```
The run **refuses** (`refusal_gate: frozen_inputs`) if any required digest is
missing or if `embedding_and_index_digest` changed after the held-out split.

---

## Sub A — Slicer spine (prove on a trivial stage before any encode)

**AC-A1 — Catalog governs I/O (truthful, not over-claimed).** `catalog.yaml` names
every dataset (name, path, format, schema, `raw|derived`). The runner supplies
**only** catalog-resolved paths, validates declared inputs/outputs, opens raw
read-only where supported, and **refuses to publish an unregistered output**.
*Oracle:* unregistered declared output → fail before write; raw opened read-only.
**Not claimed:** that stage code cannot touch an unnamed path (no sandbox).

**AC-A2 — Idempotent skip on a complete, verified key (L2).** Skip key binds:
`input_digest, params_digest, git_sha, stage_impl_version, catalog_digest,
schema_version, embedding_model_revision, seed, dep_lock_digest`. Skip only when
recorded output digests match **and** a completion marker exists. Write protocol:
`tmp → fsync → digest → atomic rename → append completed entry`.
*Oracle:* change any key component → re-run; kill mid-write → partial `.tmp` never
counts complete; unchanged → zero work, `skipped`.

**AC-A3 — Capacity gate: cgroup-effective, group-wide, enforced (L1).** The probe
derives **effective** capacity from the cgroup, not the host:
```
effective_memory = min(host_MemAvailable, cgroup.memory.max − cgroup.memory.current)
effective_cpu    = cgroup cpu.quota / cpuset   (NOT nproc alone)
```
It builds a **stage-specific** model (filter: streaming O(1); embed: model-load +
per-worker + batch + output-shard; index-build: vector matrix N·dim·sizeof(dtype)
+ index structure + temp; retrieve: query batch + result + dedup), combining
**exact static calc** (vector matrix) + **empirical measurement** (model load, ANN
structure, ORT arena) + **declared safety factor**. The full run is **refused
before expensive work** (`refusal_gate: capacity`) if projected peak > effective
budget or projected wall-clock > threshold, emitting the numbers. Measured peak is
**whole-cgroup / process-tree** memory (`memory.peak` where available), **not**
parent RSS or a heartbeat sample.
*Oracle (branch-sensitive):* tiny budget → refuse with projection; **if hard
enforcement is available** (`MemoryMax` on the run), a deliberate over-budget
allocation is **killed by the cap**; **if only monitor-and-terminate is
available**, that path terminates the run on breach — the oracle tests whichever
branch the contract declares, not always hard-cap.

**AC-A3b — Worker/thread config measured, not hardcoded.** The probe **chooses and
records** `(workers, threads_per_worker, batch)` from a bounded candidate set; the
runner caps BLAS/OpenMP/ORT threads so `workers × threads ≤ effective_cpu` — **no
oversubscription** (the interaction that reproduced the incident). No hardcoded
"256"/"8 concurrent".
*Oracle:* receipt records the chosen config; observed concurrency ≤ effective_cpu.

**AC-A4 — Two-object observability; heartbeat round-trip self-verified (L3).**
- **local checkpoint state** — durable on the box, **authoritative for resume**.
- **remote heartbeat** — force-pushed to `refs/heads/slicer-status/<run_id>` (a
  status ref, **not** a memory box), **authoritative for "alive?"**, carrying a
  **monotonic `heartbeat_seq`** (+ `stage, shard i/N, rows, cgroup_mem, status`).

Before any expensive stage the runner performs a **remote round-trip self-check**:
`publish initial heartbeat → independently fetch the remote ref → verify run_id +
heartbeat_seq + digest match → only then start`. This proves the external channel
works **without** requiring δ to be online (consistent with no always-on agent);
δ polls the ref afterward. Policy: fixed cadence, `stale_after` threshold, terminal
update, **fail-closed** — if publish or the round-trip fails, the run **refuses**
(`refusal_gate: observability`).
*Oracle:* δ polling sees advancing `heartbeat_seq` + true stage/rows mid-run and
terminal status after; block the push → the run refuses to enter the expensive
stage; a cached/stale ref is detectable via `heartbeat_seq`, not `updated_at`.

**AC-A5 — sample|full, one code path; oracle corrected (L1).** Same
implementations/schemas/params/normalization; sample = **deterministic restriction
of the input universe** (seeded). **Filter and embedding outputs agree
record-for-record** with the full run. **Retrieval is interpreted only within the
selected sample universe and is NOT claimed a projection of full-corpus
retrieval.**
*Oracle:* shared records → identical filter verdict + embedding vector; retrieval
compared only within-universe.

**AC-A6 — Resume applies to EVERY projected-long stage (L2).** General rule:
**any stage whose probe projects `> max_uncheckpointed_work_s` must expose a
resumable checkpoint boundary or be refused.** Concretely:
- embed → immutable per-shard `.npy` + manifest `{digest, row_range}` (no shared
  memmap mutation); resume = skip digest-verified shards.
- index-build, retrieve (query shards), candidate-manifest emit → each declares its
  checkpoint boundary or the probe refuses the stage.
`max_uncheckpointed_work_s` is a **declared threshold**, and each long stage has a
stage-by-stage resume proof.
*Oracle:* `SIGKILL` mid-stage in embed **and** in retrieve → each resumes from its
last checkpoint; a long stage with no checkpoint boundary is refused, not run.

**AC-A7 — Receipt (Sub C) sufficient for δ to accept from receipt + its
content-addressed references alone.**

---

## Sub B — Slicer stages (on the proven spine)

**AC-B0 — Retrieval-input blindness (before embedding).** The text embedded is a
rendered representation over an **explicit allowlist**, hashed:
```yaml
retrieval_input:
  allowed:   [title, description, normative_rules, resolution_source,
              temporal_fields, void_conditions, settlement_domain]
  forbidden: [prices, volume, outcome, resolution_result, resolver_status, comments]
  rendered_template_digest:
```
*Oracle:* the rendered representation is built **only** from allowlisted keys
(recursive JSON-key validation against the allowlist — **not** string-grep, since a
contract may legitimately contain the word "price"/"score"); its digest is
recorded.

**AC-B1 — filter + attrition receipt (L4, observation boundary).** Streams corpus →
clean-binary; peak RSS O(1). Emits attrition receipt `{total_input, included,
excluded_by_reason{...}, predicate_version, predicate_digest,
included_audit_sample(seeded), excluded_audit_sample(seeded)}`. The `vol>=$10k`
boundary is a **predeclared tradability scope, not evidence**. **Volume remains
only in evaluation metadata and never reaches retrieval or classification.**
*Oracle:* excluded-by-reason sums to `total_input − included`; recursive key-check
confirms no `volume`/`price`/`outcome` in the retrieval-input or classifier-input
schemas (only in `evaluation_manifest.jsonl`).

**AC-B2 — embed: immutable sharded, probe-configured (L1/L2/L4).** Shards encode
under the probe-chosen config (AC-A3b); each shard immutable `.npy` + manifest
(AC-A6); peak (whole-cgroup) within the enforced budget (AC-A3).
*Oracle:* probe passes; full N completes **or** probe refuses with numbers (both
pass DoD); mid-encode kill resumes at shard boundary; cgroup peak ≤ budget.

**AC-B3 — retrieve: control-recall THEN baseline subtraction; correct recall
oracle.** The order is mandatory and produces **two separate artifacts**:
```
1. ANN raw top-k neighbors over the frozen corpus
2. retrieval_control_evaluation:                    # BEFORE subtraction
   - computational_recall = compare approx top-k to EXACT neighbors of a fixed
     seeded set of m query contracts against all N vectors   # O(m·N), small m
   - relation_control_recall = held-out known-positive recovery within top-k
3. canonicalize + deduplicate (canonical directed→undirected pair id)
4. subtract frozen mechanical baseline:
     novel(p) := cross_event(p) AND pair_id(p) ∉ frozen_baseline_pair_set
5. emit novel_candidate_manifest
```
Control recall is measured on the **raw** neighbor set (step 2), **before**
subtraction — otherwise the controls (baskets/ladders, themselves baseline
members) are removed by construction and recall reads 0 spuriously.

**Relation-bearing recall is defined precisely:** denominator = held-out
known-positive pairs whose **both** members survive the clean-binary filter;
a positive counts as recovered iff its **exact counterpart** appears within top-k
of **either** member (direction-symmetric); controls used for calibration are
**excluded** from the reported held-out denominator.

The old three predicates (same-event / same-ladder / ≥2-shared-entities) are
**descriptive features in the manifest only**, never the novelty definition.
**Removed** (mathematically invalid): the prior "if exact, prove non-quadratic in
wall-clock" clause. Exact retrieval is used **only** as the bounded `O(m·N)`
reference in step 2; full-corpus exact all-query is out of scope.
*Oracle:* every emitted pair satisfies `novel`; receipt records both recall numbers
+ `m`, `k`, seed, full `retrieval` block; `retrieval_control_evaluation` and
`novel_candidate_manifest` are distinct artifacts with distinct digests.

---

## Sub C — Emit + receipt (the δ handoff)

**AC-C1 — Strict blinded classifier allowlist.** `classification_input.jsonl`
contains **only** `pair_id` + per side `{contract_id, title, description,
normative_rules, resolution_source, temporal_fields, void_conditions,
settlement_domain, text_digest, text_observed_at, version_basis,
historical_discoverability: not_established}`. **Excludes** similarity, rank,
retrieval channel, candidate reason, stratum, baseline membership, known-positive
status, prices, volume, outcome, resolver result — those live in
`evaluation_manifest.jsonl`. **"Ground truth" exists only for controls**; a novel
candidate does not acquire ground truth from a manifest row.
*Oracle:* recursive JSON-key validation of the classifier input against the
allowlist → no extra key (not string-grep).

**AC-C2 — Bounded output: full manifest vs review fixture.**
- **full candidate manifest** — box-side, **not committed to Git**, digested.
- **bounded review fixture** — deterministic stratified sample, small.
Specify + record: canonical pair dedup, max candidates per contract, semantic
bands, deterministic selection seed, review fixture size, artifact location+digest.
*Oracle:* fixture size = declared; selection reproducible from seed; full manifest
referenced by digest, absent from Git.

**AC-C3 — Receipt complete; two status axes; L5/L6 measurable.** The receipt is
reproducible + interpretable:
```yaml
run_id: · mode: · git_sha: · catalog_digest: · schema_version:
corpus_manifest_digest: · clean_binary_predicate_digest: · input_selection_digest:
retrieval_input_digest: · embedding_model: {id, revision, dimension, dtype, input_template_digest}
retrieval: {algorithm, implementation_version, metric, k, parameters, seed, search_claim}
embedding_and_index_digest: · mechanical_baseline_digest: · control_split_digest:
classification_fixture_selection_digest:
effective_capacity: {memory_max_mb, memory_available_at_probe_mb, cpu_quota, cpuset}
probe: {measured:{sample_n, peak_cgroup_mem_mb, rows_per_s, chosen_workers, chosen_threads, chosen_batch},
        projected_full:{peak_cgroup_mem_mb, wall_clock_min}, verdict: pass|refuse, reason}
recall: {computational, relation_control}          # from retrieval_control_evaluation
instrument_gates: {computational_recall_gate: pass|fail, relation_control_recall_gate: pass|fail}
novel_relation_yield:                              # measured result, not a gate
outputs: [{name, path, rows, digest}]
remote_state_sink: refs/heads/slicer-status/<run_id>
heartbeat_policy: {cadence_s, stale_after_s, seq_final, roundtrip_verified: bool}
resources: {cgroup_memory_peak_mb, wall_clock_s, shards_done, shards_total}
invariants_self_check: {measured_before_scale, resumable, observable, memory_bounded,
                        errors_surfaced, failure_is_cheap}   # all cross-checkable
failure_cost: {probe_wall_clock_s, rows_processed_before_refusal,
               max_recomputed_shards_after_kill, max_uncheckpointed_work_s}
# ---- two status axes ----
instrument_status:      accepted | rejected
full_discovery_status:  completed | capacity_refused | observability_refused | frozen_inputs_refused | not_run
refusal_gate:           capacity | observability | frozen_inputs | catalog | null
resume_point: · run_state_digest: · notes:
```
**L5 oracle (explicit):** an injected mid-stage error surfaces a classified error
in heartbeat + receipt (`errors_surfaced=true`), never a silent skip.
**δ acceptance:** `instrument_status: accepted` iff outputs+digests verify **and
both instrument gates pass**. `full_discovery_status` is orthogonal — a
`capacity_refused` run can be `instrument_status: accepted` (L1 proven) while the
census is **not** done and the central question stays underdetermined. `partial`
is never a DoD. "Receipt alone" = receipt + its content-addressed references.

---

## Definition of done (master)
- A, B, C accepted; `mode=sample` emits blinded fixtures + attrition receipt +
  both recall numbers with `instrument_status: accepted`.
- A `mode=full` dispatch is `instrument_status: accepted` when either
  `full_discovery_status: completed` **or** a predeclared gate refused with numbers
  — but the two axes are reported separately (L1 proven ≠ census done).
- Each L1–L6 has an AC+oracle; `failure_is_cheap` measured via `failure_cost`.
- Retrieval preregistered (calibration/held-out); config frozen before held-out
  eval; both instrument gates evaluated; novelty = baseline subtraction.
- Acceptance language binds **the measured clean-binary N** (to `corpus_manifest`
  + `clean_binary_predicate` digests); **≈222,855 is an expected sanity value
  only**, not a hardcoded requirement.

## Known debt (what this does NOT establish)
Non-existence of non-retrieved relations (heuristic can't prove absence) · that any
candidate is an identified relation or tradeable incoherence (downstream) ·
full-corpus exact retrieval (out of scope) · that the semantic-retrieval census was
performed when a run safely refuses (`full_discovery_status ≠ completed`).

## Provenance
```yaml
source_artifacts_sha:  # commit where RCA + design + this issue are all committed
bundle_commit_sha:     # commit where the review bundle itself landed
```
(These disambiguate the two SHAs the prior bundle header conflated.)
