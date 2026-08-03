# CMP Slicer — Review Bundle v2

**Bundle version:** v2  ·  Part 3 = Slicer issue **v2.2** (post 3rd review)
**Assembled:** 2026-08-03T01:25:20Z

## Provenance (representation — no self-reference)
```yaml
source_artifacts_sha: 792f65dead1f132a7957cd76a2f7639a22f10edc   # authoritative: the commit these three parts are taken from
bundle_parent_sha:    792f65dead1f132a7957cd76a2f7639a22f10edc
bundle_path:          docs/design/slicer-review-bundle-v2.md
# the bundle's own containing-commit SHA is recorded EXTERNALLY (this delivery /
# issue comment), never self-embedded — a file cannot authenticate its own commit.
repo:   usurobor/cmp
branch: claude/cmp-experiment-001-issue-c1oov4
```
All three parts below are the exact bytes at `source_artifacts_sha` (792f65d).

## What this is
Self-contained review artifact — no repo access needed.

| Part | Artifact | Role | Version |
|---|---|---|---|
| 1 | RCA — 2026-08-01 embed memory blowup | why the design is shaped this way | as committed |
| 2 | Design — compute pipeline architecture | invariants + rationale (executables cede to Part 3) | + L4 restated + authority note |
| 3 | Slicer issue | dispatchable executable contract | **v2.2** |

On any executable specific, **Part 3 supersedes Part 2.** Status: design only,
nothing built (RCA freeze holds; dispatch of Sub A pre-authorized on this commit).

## Changelog (issue v2.1 → v2.2, by the 3rd review's items)
1. L4 restated: per-stage declared memory bound, not corpus-independence.
2. Four status axes + prerequisite_receipts (Sub A acceptable w/ retrieval
   not_evaluated; refused full run inherits validity only via matching sample receipt).
3. Generic (Sub A, synthetic long stage) vs stage-specific (Sub B) oracle split.
4. instrument_gate_policy frozen before held-out eval; per-stratum recall; attempt
   lineage; novel_relation_yield -> novel_candidate_yield.
5. Capacity: effective_cpu=min(cpuset,quota/period); memory.high+max admission;
   swap_policy (disabled default).
6. slicer-status/** triggers no workflow (Sub A oracle) + status-ref cleanup.
7. Scientific vs execution params; embedding_equivalence {mode,atol,rtol}.

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
- **L4 — Memory bounded by a declared workload model and enforced budget.**
  Shardable stages (filter, embed) must have working memory bounded by shard/batch
  size. Global structures (e.g. the ANN index) may scale with the frozen corpus N,
  but their complete memory function must be **statically calculated or empirically
  bounded before admission**, and no stage may accumulate undeclared/unbounded
  per-record state. Formally `peakMem(stage) ≤ B_stage(N, shard, config) ≤ enforced
  budget`. (The old "= f(shard), never f(corpus)" was false for the in-memory
  ANN index, which is necessarily f(N); the invariant is *no surprise
  accumulation*, not corpus-independence.)
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
| OOM / swap (3× today) | declared per-stage memory bound + enforced budget + capacity probe (shardable stages bounded by shard; global structures bounded before admission) |
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

# PART 3 — Executable contract: Slicer issue v2.2

_Source: `docs/design/slicer-issue-draft.md`_

# Build Slicer v2.2 — CMP candidate-retrieval instrument (spine-first, runner-cell)

**Mode:** design-and-build.
**Source of truth:** `docs/design/compute-pipeline-architecture.md` (invariants +
rationale) + RCA `threads/rca/2026-08-01-embed-memory-blowup.md`. **Authority:**
this issue owns all *executable specifics*; where the design doc and this issue
differ on an executable specific, **this issue supersedes**.
**Dispatch target:** one ephemeral Slicer run on the cn-sigma self-hosted runner
(the "cell" = the run; filter/embed/retrieve are **stages, not agents**). δ
dispatches; the run emits a receipt; δ accepts/rejects.

**Governing invariants (L1–L6); each AC has a file+check+pass/fail oracle:**
> L1 measure-before-scale · L2 resumable (every projected-long stage) · L3
> observable-without-privilege (silence falsifiable) · **L4 memory bounded by a
> declared workload model + enforced budget** — `peakMem(stage) ≤ B_stage(N,
> shard, config) ≤ enforced budget`; shardable stages bounded by shard/batch,
> global structures (ANN index) may be f(N) but must be statically-calculated or
> empirically-bounded **before admission**; no undeclared per-record accumulation
> · L5 errors-surface · L6 failure-is-cheap (measured).

## What Slicer is — and is not (TSC framing)
α-like observation/filter + **early β-like heuristic candidate search**. Output =
**candidate pairs**, never *identified relations* or *tradeable incoherences*. A
run establishes only: *under this frozen boundary + declared heuristic method,
these candidate pairs surfaced reproducibly.* Full CMP sequence: `Slicer →
blinded joint-state classification → counterexample review → SAT/SMT atlas →
frozen law → price LP → trade witness.`

## Non-goals
Classifier/verification · **exact full-corpus all-query retrieval** (the excluded
O(n²)) · pairwise/global stage · price/volume/outcome in retrieval OR
classification · SAT/SMT/LP · DuckDB/Polars (deferred) · heavyweight orchestrator ·
persistent box-agent · claiming filesystem confinement without a sandbox.

## Decomposition (tracer-bullet; generic vs stage-specific oracles)
- **Sub A — Spine.** Prove the **generic** mechanisms (capacity gate + refusal,
  cgroup budget/swap, heartbeat round-trip, idempotency, checkpoint/resume, error
  surfacing) against a **synthetic checkpointed long stage** — **no embedding or
  ANN**. Sub A is acceptably complete with `retrieval_instrument_status:
  not_evaluated`.
- **Sub B — Stages.** Re-prove those same properties **specifically** for filter,
  embed, index-build, retrieve, emit — each heavy stage admitted only after its own
  oracle passes.
- **Sub C — Emit + receipt.** Blinded fixtures + the receipt δ accepts.

## Frozen inputs (pinned + digested; recorded in the receipt)
```yaml
corpus_manifest:        {artifact, digest, market_count}      # liquid vol>=$10k
clean_binary_predicate: {version, digest}
embedding:              {model_id, model_revision, input_representation, dimension,
                         dtype, normalization, truncation_policy}
retrieval:              {algorithm, implementation_version, metric, k,
                         index_parameters, query_parameters, seed, search_claim: heuristic}
mechanical_baseline:    {artifact, digest, generation_commit, method_version, pair_canonicalization}
retrieval_input:        {allowed[], forbidden[], rendered_template_digest}   # AC-B0
control_split:          {calibration_ids_digest, heldout_ids_digest, seed}
instrument_gate_policy:  # frozen before held-out eval — see below
```
The run **refuses** (`discovery_status: frozen_inputs_refused`) if any required
digest is missing or if `embedding_and_index_digest` changed after the held-out
split was drawn.

## Preregistration & instrument-gate policy (frozen before held-out evaluation)

Controls are split calibration ⊎ held-out (disjoint, seeded). Config (embedding +
ANN + k) is selected on **calibration** controls, then **frozen** to
`embedding_and_index_digest`; the **held-out** evaluation runs **once**. The full
decision contract is itself a frozen, digested artifact:

```yaml
instrument_gate_policy:
  computational_metric:        recall_at_k
  computational_recall_min:    #
  exact_reference_query_count_m: #
  relation_control_recall_min: #                 # overall floor
  k:                           #
  exclude_self_match:          true
  tie_policy:                  #                 # e.g. stable-sort by (score, id)
  control_strata:
    - exact_or_paraphrase_equivalence
    - threshold_or_deadline_implication
    - partition_or_exclusion
    - cross_event_semantic_relation              # target-representative; include ONLY where genuine adjudicated ground truth exists — never fabricated to pass
  minimum_heldout_pairs_per_stratum: #
  per_stratum_recall_min:      {…}               # relation-control recall reported + gated BY STRATUM
  attempt_id:                  #
  prior_attempt_receipts:      []                # failed held-out attempts stay in lineage
  digest:
```
A failed held-out evaluation is **retained**; redrawing a new held-out set is a
**new `attempt_id`** referencing the prior failure — never an overwrite. Values may
be chosen during calibration but the whole policy is committed+digested **before**
the held-out run. Passing recall on easy same-event baskets/ladders does **not**
establish cross-event retrieval capability — hence per-stratum reporting.

**Gate semantics:** `computational_recall_gate` and `relation_control_recall_gate`
(per-stratum) are **instrument-validity gates**. `novel_candidate_yield` is the
**measured result, never a gate**. Low yield with gates passing = a valid negative
scientific result; a failed relation-control gate = the instrument failed and any
yield is uninterpretable.

---

## Sub A — Slicer spine (generic mechanisms on a SYNTHETIC long stage)

> All Sub A oracles run against a **synthetic checkpointed long stage** — no
> model, no ANN. Sub B re-proves them for the real stages.

**AC-A1 — Catalog governs I/O (truthful).** Runner supplies only catalog-resolved
paths, validates declared I/O, opens raw read-only where supported, refuses to
publish unregistered outputs. *Not claimed:* filesystem confinement (no sandbox).
*Oracle:* unregistered declared output → fail before write.

**AC-A2 — Idempotent skip on a complete, verified key (L2).** Key binds
`input_digest, params_digest, git_sha, stage_impl_version, catalog_digest,
schema_version, embedding_model_revision, seed, dep_lock_digest`; skip only on
matching output digests **and** completion marker; write `tmp→fsync→digest→atomic
rename→append completed`. *Oracle:* change any component → re-run; kill mid-write →
`.tmp` never counts complete; unchanged → `skipped`.

**AC-A3 — Capacity gate: cgroup-effective, group-wide, enforced, swap-bound (L1/L4).**
Effective capacity is derived from the cgroup, not the host:
```
quota_cores    = cpu.max.quota / cpu.max.period        # ignore when quota == "max"
effective_cpu  = min(cpuset_cpu_count, quota_cores)
available_mem  = min( host_MemAvailable,
                      memory.high - memory.current  (if finite),   # reclaim/throttle boundary
                      memory.max  - memory.current  (if finite) )  # kill boundary
                × safety_factor
```
`memory.high` is included because reaching it induces the reclaim/throttle that
was the RCA's symptom, before `memory.max` kills. **Swap is bound**:
```yaml
swap_policy: {mode: disabled | bounded | monitor_and_terminate,
              memory_swap_max_mb, swap_at_probe_mb, swap_termination_threshold_mb}
```
`disabled` is the default where the runner permits it — a process silently
surviving 70 h by swapping does **not** count as within budget. Per-stage
`B_stage(N, shard, config)` model (filter streaming; embed model-load+worker+batch+
shard; index-build vector-matrix `N·dim·sizeof(dtype)`+structure+temp; retrieve
query+result+dedup) combines exact static calc + empirical measurement + safety
factor. The full run is refused before expensive work (`discovery_status:
capacity_refused`) if projected peak > `available_mem` or wall-clock > threshold.
Measured peak is **whole-cgroup** (`memory.peak`/`memory.current` high-water), not
parent RSS or a heartbeat sample.
*Oracle (branch-sensitive):* tiny budget → refuse with the projection; **hard
enforcement available** → deliberate over-budget alloc is killed by the cap;
**monitor-and-terminate only** → that path terminates on breach; swap use beyond
`swap_termination_threshold_mb` terminates. The oracle tests the declared branch.

**AC-A3b — Worker/thread config measured, not hardcoded.** Probe chooses+records
`(workers, threads_per_worker, batch)` from a bounded set; runner caps
BLAS/OMP/ORT so `workers × threads ≤ effective_cpu` — no oversubscription.
*Oracle:* receipt records config; observed concurrency ≤ effective_cpu.

**AC-A4 — Two-object observability; heartbeat round-trip self-verified; no
workflow re-trigger (L3).**
- **local checkpoint state** — durable, resume-authoritative.
- **remote heartbeat** — force-pushed to `refs/heads/slicer-status/<run_id>` (status
  ref, not a memory box), alive-authoritative, monotonic `heartbeat_seq`.
Before any expensive stage the runner does a **remote round-trip self-check**:
`publish → independently fetch the ref → verify run_id+seq+digest → start`. Proves
the channel without an always-on δ. Fail-closed: publish/round-trip failure →
`discovery_status: observability_refused`. **`slicer-status/**` must trigger no
workflow** (else each heartbeat could re-dispatch and consume the very runner it
observes); a declared cleanup policy governs terminal status-ref
retention/deletion.
*Oracle:* δ polling sees advancing `heartbeat_seq` mid-run + terminal after;
blocking the push → run refuses to enter the expensive stage; **publishing a test
heartbeat enqueues zero workflows** (proven in Sub A); a stale ref is detectable
via `heartbeat_seq`, not `updated_at`.

**AC-A5 — sample|full one code path; scientific vs execution params (L1).**
- **scientific params** (model_revision, rendered text, normalization, truncation,
  dtype) — **identical** across sample and full.
- **execution params** (workers, threads, batch, shard size) — may differ under the
  probe.
Sample = deterministic seeded restriction of the input universe. Filter verdicts
agree record-for-record; **embedding equality** is tested under a declared mode:
```yaml
embedding_equivalence: {mode: exact | allclose, atol, rtol}
```
(`exact` only under a declared deterministic execution mode; otherwise tolerance.)
**Retrieval is interpreted only within the sample universe — not a projection of
full-corpus retrieval.**
*Oracle:* shared records → identical filter verdict + embedding within declared
equivalence; retrieval compared only within-universe.

**AC-A6 — Resume applies to EVERY projected-long stage (L2).** Any stage whose
probe projects `> max_uncheckpointed_work_s` (declared threshold) **must expose a
resumable checkpoint boundary or be refused** (`discovery_status:
resumability_refused`). Proven generically here on the synthetic long stage.
*Oracle:* `SIGKILL` mid-synthetic-stage → resumes from last checkpoint; a
would-be-long stage without a checkpoint boundary is refused, not run.

**AC-A7 — Error surfacing (L5).** Injected mid-stage error → surfaced, classified
error in heartbeat + receipt (`errors_surfaced=true`), never a silent skip.
*Oracle:* inject a fault → the run reports it, does not "look quiet".

---

## Sub B — Real stages (re-prove the generic properties per stage)

**AC-B0 — Retrieval-input blindness (before embedding).** Embedded text is a
rendered representation over an explicit allowlist, hashed:
```yaml
retrieval_input:
  allowed:   [title, description, normative_rules, resolution_source,
              temporal_fields, void_conditions, settlement_domain]
  forbidden: [prices, volume, outcome, resolution_result, resolver_status, comments]
  rendered_template_digest:
```
*Oracle:* recursive JSON-key validation against the allowlist (**not** string-grep —
a contract may legitimately contain "price"/"score"); digest recorded.

**AC-B1 — filter + attrition receipt (L4 observation boundary).** Streams corpus →
clean-binary; working memory bounded by batch. Attrition receipt `{total_input,
included, excluded_by_reason{…}, predicate_version, predicate_digest,
included_audit_sample(seeded), excluded_audit_sample(seeded)}`. `vol>=$10k` is a
predeclared tradability scope, not evidence; **volume stays only in
`evaluation_manifest.jsonl` — never in retrieval-input or classifier-input.**
*Oracle:* excluded-by-reason sums to `total_input − included`; recursive key-check
finds no volume/price/outcome in retrieval-input or classifier-input schemas.

**AC-B2 — embed: immutable sharded, probe-configured; per-stage resume (L1/L2/L4).**
Immutable per-shard `.npy` + manifest `{digest, row_range}` (no shared-memmap
mutation); probe-chosen config (A3b); whole-cgroup peak within budget (A3).
Re-proves AC-A3/A6/A7 **for embed specifically** before admission.
*Oracle:* probe passes; full N completes or refuses with numbers; `SIGKILL`
mid-embed resumes at shard boundary; cgroup peak ≤ budget.

**AC-B3 — retrieve: index-build bounded; control-recall THEN subtraction; correct
oracle.** Index build is admitted under its declared `B_index(N,dim,dtype,params)`
bound (L4) and its own resume/refuse rule (A6). Then, in mandatory order,
producing **two artifacts**:
```
1. ANN raw top-k over the frozen corpus
2. retrieval_control_evaluation  (BEFORE subtraction):
   - computational_recall = approx top-k vs EXACT neighbors of a fixed seeded set
     of m query contracts against all N vectors        # O(m·N), small m — the ONLY exact use
   - relation_control_recall = held-out known-positive recovery within top-k, BY STRATUM
3. canonicalize + deduplicate (directed→canonical undirected pair id)
4. novel(p) := cross_event(p) AND pair_id(p) ∉ frozen_baseline_pair_set
5. emit novel_candidate_manifest
```
Control recall is on the **raw** neighbor set (before subtraction) — else the
controls (baskets/ladders, themselves baseline members) are removed by
construction. **Relation-bearing recall definition:** denominator = held-out
known-positive pairs whose **both** members survive the filter; recovered iff the
**exact counterpart** appears within top-k of **either** member (direction-
symmetric); **calibration controls excluded** from the held-out denominator.
**Removed** (invalid): any "prove exact non-quadratic in wall-clock" clause —
full-corpus exact all-query is out of scope; exact appears only as the bounded
`O(m·N)` reference in step 2.
*Oracle:* every emitted pair satisfies `novel`; `retrieval_control_evaluation` and
`novel_candidate_manifest` are distinct digested artifacts; both recalls (per
stratum) recorded with `m,k,seed` and full `retrieval` block.

---

## Sub C — Emit + receipt (the δ handoff)

**AC-C1 — Strict blinded classifier allowlist.** `classification_input.jsonl` =
only `pair_id` + per side `{contract_id, title, description, normative_rules,
resolution_source, temporal_fields, void_conditions, settlement_domain,
text_digest, text_observed_at, version_basis, historical_discoverability:
not_established}`. Excludes similarity/rank/channel/reason/stratum/baseline-
membership/known-positive/prices/volume/outcome/resolver — those live in the
manifest. **Ground truth exists only for controls.**
*Oracle:* recursive JSON-key validation vs the allowlist → no extra key.

**AC-C2 — Bounded output.** full candidate manifest (box-side, not in Git, digested)
vs bounded review fixture (deterministic stratified sample). Record: canonical
dedup, max candidates/contract, semantic bands, selection seed, fixture size,
location+digest. *Oracle:* fixture size = declared; selection reproducible; full
manifest by digest, absent from Git.

**AC-C3 — Receipt complete; FOUR status axes; L5/L6 measurable.** Status is
decomposed so Sub A, sample validation, and a safely-refused full run are each
truthfully representable:
```yaml
execution_status:            completed | refused | partial | failed
contract_status:             accepted | rejected
retrieval_instrument_status: not_evaluated | accepted | rejected
discovery_status:            not_run | sample_completed | full_completed |
                             capacity_refused | observability_refused |
                             frozen_inputs_refused | resumability_refused | catalog_refused
prerequisite_receipts:       [receipt_digest]      # e.g. the accepted sample receipt a refused full run inherits validity from
```
Truthful examples:
- **Sub A ok:** execution completed · contract accepted · retrieval not_evaluated · discovery not_run.
- **Sample validates instrument:** execution completed · contract accepted · retrieval accepted · discovery sample_completed.
- **Full safely refuses:** execution refused · contract accepted · retrieval accepted · discovery capacity_refused · prerequisite_receipts:[sample receipt digest].
A full run may inherit `retrieval_instrument_status: accepted` **only** by
referencing a prior accepted sample receipt with **matching** corpus / filter /
embedding / ANN / gate-policy digests. `partial` is never a DoD.

Reproducibility + measurement fields:
```yaml
git_sha · catalog_digest · schema_version · corpus_manifest_digest
clean_binary_predicate_digest · input_selection_digest · retrieval_input_digest
embedding_model:{id,revision,dimension,dtype,input_template_digest}
retrieval:{algorithm,implementation_version,metric,k,parameters,seed,search_claim}
embedding_and_index_digest · mechanical_baseline_digest · control_split_digest
instrument_gate_policy_digest · classification_fixture_selection_digest
effective_capacity:{cpuset_count, quota_cores, effective_cpu,
                    memory_high_mb, memory_max_mb, memory_available_at_probe_mb,
                    swap_policy, swap_at_probe_mb}
probe:{measured:{sample_n, peak_cgroup_mem_mb, rows_per_s, chosen_workers,
                 chosen_threads, chosen_batch}, projected_full:{peak_cgroup_mem_mb,
                 wall_clock_min}, verdict: pass|refuse, reason}
recall:{computational, relation_control_by_stratum:{…}}
instrument_gates:{computational_recall_gate: pass|fail,
                  relation_control_recall_gate_by_stratum:{…}}
novel_candidate_yield:{raw_directed_neighbors, canonical_pairs,
                       baseline_subtracted_pairs, cross_event_pairs,
                       pairs_per_contract_distribution}   # measured result — NOT validated relations
outputs:[{name,path,rows,digest}]
remote_state_sink: refs/heads/slicer-status/<run_id>
heartbeat_policy:{cadence_s, stale_after_s, seq_final, roundtrip_verified, status_ref_cleanup}
resources:{cgroup_memory_peak_mb, wall_clock_s, shards_done, shards_total}
invariants_self_check:{measured_before_scale, resumable, observable, memory_bounded,
                       errors_surfaced, failure_is_cheap}
failure_cost:{probe_wall_clock_s, rows_processed_before_refusal,
              max_recomputed_shards_after_kill, max_uncheckpointed_work_s}
resume_point · run_state_digest · notes
```
**`novel_candidate_yield`** (not "relation yield"): Slicer surfaces *candidates*;
`validated_relation_yield` is computed downstream by blind classification + review.
**L5 oracle:** injected error → surfaced/classified, never silent.
**δ acceptance:** `contract_status: accepted` iff outputs+digests verify, the
declared invariants cross-check the heartbeat/manifests, and — **when retrieval was
evaluated** — both instrument gates pass; `retrieval_instrument_status:
not_evaluated` is legitimate for Sub A. "Receipt alone" = receipt + its
content-addressed references.

---

## Definition of done (master)
- Sub A accepted (`execution_status: completed, contract_status: accepted,
  retrieval_instrument_status: not_evaluated`) — its generic oracles pass on the
  synthetic long stage; **no embed/ANN work occurs until Sub A is accepted.**
- Sub B re-proves the generic properties per real stage; a sample run reaches
  `retrieval_instrument_status: accepted, discovery_status: sample_completed` with
  both gates passing.
- A full run reports the four axes truthfully; `full_completed` **or** a
  predeclared `*_refused` with numbers both keep `contract_status: accepted` (L1),
  and a refused full run inherits instrument validity only via a matching-digest
  sample receipt.
- Each L1–L6 has an AC+oracle; `failure_is_cheap` measured via `failure_cost`.
- Acceptance binds **the measured clean-binary N** (to `corpus_manifest` +
  `clean_binary_predicate` digests); **≈222,855 is an expected sanity value only.**

## Known debt (what this does NOT establish)
Non-existence of non-retrieved relations · that any candidate is an identified
relation or tradeable incoherence (downstream) · full-corpus exact retrieval (out
of scope) · that the census was performed when a run safely refuses
(`discovery_status ≠ full_completed`).

## Provenance (representation)
```yaml
source_artifacts_sha:  # commit these three parts are taken from (authoritative content)
bundle_parent_sha:     # == source_artifacts_sha
bundle_path:           docs/design/slicer-review-bundle-v2.md
# the bundle's own containing-commit SHA is recorded EXTERNALLY (issue comment /
# review receipt), never self-embedded (a file cannot authenticate the commit that
# contains itself).
```
