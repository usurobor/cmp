# CMP Slicer — Review Bundle v1

**Bundle version:** v1
**Assembled:** 2026-08-03T00:21:23Z
**Repo state:** usurobor/cmp @ 35ae651 (35ae65173a7cdfc3e0c4a7fa8cf4fcbca49a8dab), branch claude/cmp-experiment-001-issue-c1oov4
**Purpose:** a single self-contained artifact for external review — the reviewer
does not need repo access. It carries the full chain: the incident that motivates
the design, the design (source of truth), and the executable contract.

## What this is and how to read it

The prior review of the Slicer issue could not check consistency against the
upstream design and RCA because they were not attached. This bundle fixes that:
all three artifacts are here, in dependency order.

| Part | Artifact | Role | Component version |
|---|---|---|---|
| 1 | RCA — 2026-08-01 embed memory blowup | why the design is shaped this way | as committed |
| 2 | Design — compute pipeline architecture | source of truth (invariants, stack, framework research) | includes 8vCPU/16GB envelope |
| 3 | Executable contract — Slicer issue | dispatchable contract (ACs + oracles + receipt) | **v2** (post-review amendments) |

Reading order is top-to-bottom: Part 1 names the failure class; Part 2 turns it
into invariants + a stack; Part 3 binds it into a testable contract. Part 3 is the
thing that gets dispatched; Parts 1–2 are its justification and source of truth.

Status: **design only, nothing built** (RCA remediation freeze holds). Dispatch is
gated on explicit freeze-lift by the operator.

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

# PART 2 — Design: CMP compute pipeline (source of truth)

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

# PART 3 — Executable contract: Slicer issue (v2)

_Source: `docs/design/slicer-issue-draft.md` — amended against the prior review's
five blockers + contract repairs._

# Build Slicer v2 — CMP candidate-retrieval instrument (spine-first, runner-cell)

**Mode:** design-and-build.
**Source of truth:** `docs/design/compute-pipeline-architecture.md` + RCA
`threads/rca/2026-08-01-embed-memory-blowup.md`. This issue is the executable
contract; the consequential algorithm (retrieval) is bound **here**, not deferred
to the design.
**Dispatch target:** one ephemeral Slicer run on the cn-sigma self-hosted runner
(the "cell" = the run; filter/embed/retrieve are **stages, not agents**). δ
dispatches; the run emits a receipt; δ accepts/rejects.
**Governing contract:** invariants **L1–L6** (below). Every AC serves one; each has
a file+check+pass/fail oracle.

> L1 measure-before-scale · L2 resumable · L3 observable-without-privilege
> (silence falsifiable) · L4 memory-bounded-by-construction · L5 errors-surface ·
> L6 failure-is-cheap (measurable, not asserted).

## What Slicer is — and is not (TSC framing)

Slicer performs an **α-like observation/filter** and an **early β-like heuristic
relation-candidate search**. It does **not** infer permitted joint states,
establish relation standing, build the atlas or the law, or detect price
incoherence. Its output is therefore **candidate pairs**, never *identified
relations* or *tradeable incoherences*.

Its search claim is heuristic and bounded:
```yaml
relation_search_claim:
  kind: heuristic
  bounded_surface:            # corpus boundary + filter predicate digest
  embedding_and_index_digest: # model + ANN config
  k:
  controls:                   # injected known-positives
```
A successful run establishes only: *under this frozen corpus boundary and this
declared heuristic retrieval method, these candidate pairs were surfaced
reproducibly.* **It does not establish that non-retrieved relations do not exist.**
A negative result reads "this embedding+index+config did not surface it," never
"semantic retrieval does not work."

Full CMP sequence (Slicer is only the first hop):
`Slicer candidate retrieval → blinded permitted-joint-state classification →
counterexample review → global SAT/SMT atlas → frozen joint-settlement law →
price-realizability LP → executable trade witness.`

## Non-goals
Classifier/verification (downstream, sandbox) · exact all-pairs O(n²) retrieval ·
the pairwise/global constraint stage · price/volume/outcome exposure to the
classifier · SAT/SMT/LP/prices · DuckDB/Polars-streaming (deferred until a stage
exceeds RAM) · heavyweight orchestrator · persistent box-agent · claiming
filesystem confinement without a sandbox.

## Frozen inputs (pinned + digested; recorded in the receipt)

```yaml
corpus_manifest:      {artifact, digest, market_count}        # liquid vol>=$10k
clean_binary_predicate: {version, digest}                     # the filter rule
embedding:
  model_id:           # e.g. BAAI/bge-small-en-v1.5
  model_revision:     # exact HF revision / ONNX file digest
  input_representation:  # the exact text template fed to the model
  dimension:
  dtype:              # e.g. float32
  normalization:      # e.g. L2 unit
  truncation_policy:  # max tokens + how truncated
retrieval:
  algorithm:          # e.g. hnswlib | faiss-IVF | exact-chunked — DECLARED
  implementation_version:
  metric:             # cosine | ip | l2
  k:
  index_parameters:   # e.g. M, efConstruction
  query_parameters:   # e.g. efSearch
  seed:
  search_claim:       heuristic
mechanical_baseline:  {artifact, digest, generation_commit, method_version, pair_canonicalization}
```
`corpus_manifest`, `clean_binary_predicate`, and `mechanical_baseline` are
**frozen before the run**; the run refuses if any digest is missing.

---

## Sub A — Slicer spine (prove on a trivial stage before any encode)

**AC-A1 — Catalog governs I/O (truthful, not over-claimed).** `catalog.yaml`
names every dataset (name, path, format, schema, `raw|derived`). The runner
supplies **only** catalog-resolved paths, validates every declared input/output,
opens raw inputs **read-only** where supported, and **refuses to publish an
output not registered in the catalog**.
*Oracle:* a stage declaring an unregistered output fails before writing; a raw
input is opened read-only. **Not claimed:** that arbitrary stage code cannot touch
an unnamed path (no sandbox → not mechanically enforceable → not promised).

**AC-A2 — Idempotent skip on a complete key, verified (L2).** Skip key binds
**all** of: `input_digest, params_digest, code/git_sha, stage_impl_version,
catalog_digest, schema_version, embedding_model_revision, seed, dep_lock_digest`.
A stage is skipped only when its outputs' recorded digests match **and** a
completion marker exists — never on mere file presence.
Write protocol: `tmp → fsync → digest → atomic rename → append completed entry to
the shard/stage manifest`.
*Oracle:* change any skip-key component → stage re-runs; kill mid-write → the
partial `.tmp` is never mistaken for complete (no completion entry); re-run with
all components unchanged → zero work, run-state `skipped`.

**AC-A3 — Self-calibrating gate, grounded and enforced (L1).** The probe builds a
**stage-specific** memory model, not a single number:
```
filter:      bounded streaming (O(1) in corpus)
embed:       model-load baseline + per-worker overhead + batch overhead + output-shard
index-build: vector matrix + index structure + temp construction overhead
retrieve:    query batch + result buffers + dedup buffers
```
It combines **exact static calculation** where dims are known (vector matrix =
N·dim·sizeof(dtype)), **empirical measurement** where library overhead is not
(model load, ANN index structure, ORT arena), a **declared safety factor**, and a
**hard runtime memory cap**. The full run is **refused before expensive work** if
projected peak > derived budget or projected wall-clock > threshold, emitting the
numbers. The cap is **enforced** (cgroup/`MemoryMax` on the run) — or, if
enforcement is unavailable, the claim weakens explicitly to *"monitor and
terminate on budget breach"* (no silent overrun).
*Oracle:* tiny `MEM_BUDGET` → refuse with a concrete projection; normal → pass +
record the measured model; a deliberate over-budget allocation is **killed by the
cap**, not silently tolerated.

**AC-A3b — Thread/worker config is measured, not hardcoded.** The probe **chooses
and records** the worker count and library thread caps from a bounded candidate
set; the runner explicitly caps BLAS/OpenMP/ORT threads and **must not
oversubscribe** the measured box (N workers × M library threads ≤ cores). The
prior "8 concurrent workers" hardcode is **removed** — oversubscription is exactly
the interaction that reproduces the incident.
*Oracle:* the receipt records the chosen `(workers, threads_per_worker)`; measured
concurrency never exceeds cores.

**AC-A4 — Two-object observability; silence falsifiable (L3).** Split cleanly:
- **local checkpoint state** — durable on the box, **authoritative for resume**.
- **remote heartbeat projection** — published to a git ref
  `refs/heads/slicer-status/<run_id>` (a dedicated status ref, **not** a memory
  box; force-pushed), **authoritative for "is it alive?"** δ reads it with no box
  access.
The heartbeat carries a **monotonically increasing `heartbeat_seq`** (not only
`updated_at`, so a cached read cannot look current), plus `{stage, shard i/N,
rows, peak_rss, status}`. Policy: **initial heartbeat before any heavy work**,
fixed cadence, a `stale_after` threshold, a terminal update, and **fail-closed on
publish failure** (if the heartbeat cannot be published, the expensive stage does
not start). **Hard rule:** the expensive stage may not begin until δ (the remote
observer) has **read the initial heartbeat back**.
*Oracle:* δ polling the status ref sees advancing `heartbeat_seq` + true
stage/rows mid-run and the terminal status after; block the publish → the run
refuses to enter the expensive stage rather than running unobserved.

**AC-A5 — sample|full, one code path; oracle corrected (L1).** `mode=sample(N)`
and `mode=full` invoke the **same stage implementations, schemas, parameters, and
normalization**; the sample is a **deterministic restriction of the input
universe** (seeded). **Filter and embedding outputs must agree record-for-record**
with the corresponding full-run records. **Retrieval is interpreted only relative
to the selected sample universe and is NOT claimed to be a projection of
full-corpus retrieval** (a record's neighbors within 10k need not be its neighbors
within 222,855).
*Oracle:* for records in both runs, filter verdict and embedding vector are
identical; retrieval results are compared only within-universe, never asserted
equal across universes.

**AC-A6 — Resume via immutable shard files (L2).** Embeddings are written as
**immutable per-shard files** `embeddings/shard-NNNNN.npy` with a manifest entry
`{digest, row_range}` — **not** many workers mutating one shared memmap. A later
deterministic stage exposes the complete set as one logical mmap matrix. Resume =
**skip verified-complete shard files**, never "infer trustworthy regions of a
shared file."
*Oracle:* `SIGKILL` mid-encode; restart; completed shards (digest-verified) are
skipped, only the incomplete/missing shard recomputes; no shared-file corruption
path exists.

**AC-A7 — Receipt (schema in Sub C) sufficient for δ to accept from receipt +
its content-addressed references alone.**

---

## Sub B — Slicer stages (on the proven spine)

**AC-B1 — filter + attrition receipt (L4, observation boundary).** Streams the
corpus → clean-binary subset; peak RSS O(1) in corpus size. Because "clean-binary"
is a **major observation boundary**, the stage emits an attrition receipt:
`{total_input, included, excluded_by_reason{...}, predicate_version, predicate_digest,
included_audit_sample(seeded), excluded_audit_sample(seeded)}`.
The `vol>=$10k` corpus boundary is a **predeclared tradability scope, not evidence
that a relation is true**; it is stated as such, and **volume never reaches the
classifier**.
*Oracle:* excluded-by-reason counts sum to `total_input − included`; the seeded
excluded-sample is inspectable; no volume field appears downstream of filter.

**AC-B2 — embed: immutable sharded, probe-configured (L1/L2/L4).** Shards encode
under the probe-chosen `(workers, threads)` config; `ENC_BATCH` is set from the
probe, not hardcoded; each shard is an immutable `.npy` + manifest entry (AC-A6);
peak RSS stays within the enforced budget (AC-A3).
*Oracle:* probe passes; full 222,855 completes in one pass **or** the probe
refuses with numbers (both pass DoD); mid-encode kill resumes at shard boundary;
receipt's `peak_rss` ≤ budget, cross-checked against heartbeat maxima.

**AC-B3 — retrieve: declared ANN + baseline subtraction + two recalls.** Retrieval
is the **declared** `retrieval.algorithm` (heuristic ANN unless explicitly exact),
bound by the frozen `embedding`/`retrieval` blocks. Novelty is **set subtraction
against the frozen mechanical baseline**, not proxy predicates:
```
novel_to_baseline(p) := cross_event(p)  AND  pair_id(p) ∉ frozen_mechanical_baseline_pair_set
```
(The old three predicates — same-event / same-ladder / ≥2-shared-entities — remain
only as **descriptive features in the manifest**, never as the definition.)
Two required measurements:
1. **Computational recall** — approximate-kNN vs **exact**-kNN on a bounded seeded
   sample (quantifies what the ANN misses).
2. **Relation-bearing recall** — fraction of hidden known-positive related pairs
   recovered within top-k.
*Oracle:* every emitted pair satisfies `novel_to_baseline`; the receipt records
both recall numbers, `k`, seed, and the full `retrieval` block; if `algorithm` is
exact, the probe must have proven it non-quadratic in wall-clock at N, else it is
refused.

---

## Sub C — Emit + receipt (the δ handoff)

**AC-C1 — Strict blinded allowlist.** `classification_input.jsonl` contains **only**:
```yaml
pair_id:
a: {contract_id, title, description, normative_rules, resolution_source,
    temporal_fields, void_conditions, settlement_domain,
    text_digest, text_observed_at, version_basis, historical_discoverability: not_established}
b: { ...same... }
```
It **excludes**: similarity, neighbor rank, retrieval channel, candidate reason,
batch stratum, baseline membership, known-positive status, prices, volume,
outcome, resolver result. Those live in `evaluation_manifest.jsonl`.
**"Ground truth" exists only for controls** — a novel candidate does not acquire
ground truth merely because a manifest row exists; it requires downstream
adjudication.
*Oracle:* diff the classifier-input schema against the allowlist → no extra field;
grep for price/volume/rank/score/stratum in the classifier input → none.

**AC-C2 — Bounded output: full manifest vs review fixture.** Distinguish:
- **full candidate manifest** — box-side derived artifact, **not committed to
  Git**, digested.
- **bounded review fixture** — deterministic stratified sample, small enough to
  track and classify.
Specify + record: canonical **pair deduplication**, **max candidates per
contract**, **semantic bands**, **deterministic selection seed**, **review fixture
size**, artifact **location + digest**. (At N≈222,855, even k=20 is millions of
directed neighbor records before dedup — the manifest is bounded by construction,
the fixture by selection.)
*Oracle:* fixture size = declared; selection is reproducible from the seed; the
full manifest is referenced by digest, absent from Git.

**AC-C3 — Receipt complete + status matrix (L5/L6).** The receipt is reproducible
and interpretable, not a bag of booleans:
```yaml
run_id:
mode:            sample(N) | full
git_sha:
catalog_digest:
input_selection_digest:
clean_binary_predicate_digest:
corpus_manifest_digest:
embedding_model: {id, revision, dimension, dtype, input_template_digest}
retrieval:       {algorithm, implementation_version, metric, k, parameters, seed, search_claim}
mechanical_baseline_digest:
classification_fixture_selection_digest:
outputs:         [{name, path, rows, digest}]
probe:
  measured:      {nproc, mem_available_mb, sample_n, peak_rss_mb, rows_per_s, chosen_workers, chosen_threads}
  projected_full:{peak_rss_mb, wall_clock_min}
  verdict:       pass | refuse
  reason:
recall:          {computational, relation_bearing}
remote_state_sink: refs/heads/slicer-status/<run_id>
heartbeat_policy: {cadence_s, stale_after_s, seq_final}
run_state_digest:
resources:       {peak_rss_mb, wall_clock_s, shards_done, shards_total}
invariants_self_check:            # asserted AND cross-checkable vs heartbeat/manifests
  measured_before_scale: bool
  resumable:             bool
  observable:            bool
  memory_bounded:        bool
  errors_surfaced:       bool
  failure_is_cheap:      bool     # L6 — was missing
failure_cost:                     # L6 measurable oracle
  probe_wall_clock_s:
  rows_processed_before_refusal:
  max_recomputed_shards_after_kill:
  max_uncheckpointed_work_s:
status:          done | refused | partial | failed
resume_point:
notes:
```
**L5 oracle (explicit):** an injected error mid-stage produces a surfaced,
classified error in the heartbeat + receipt (`errors_surfaced=true`), never a
silent skip.
**δ status matrix (precise):**
- `done` — accept **only** if all required outputs+digests verify.
- `refused` — accept **only** if a predeclared probe guard refused **before**
  expensive work.
- `partial` — resumable intermediate; **never** accepted as Definition of Done.
- `failed` — reject.
"Receipt alone" means **receipt + its immutable content-addressed references**, not
unaudited self-assertions.

---

## Definition of done (master)
- A, B, C accepted; a `mode=sample` dispatch emits blinded fixtures + attrition
  receipt + recall numbers with a green receipt.
- A `mode=full` dispatch either completes 222,855 in one pass **or** the probe
  refuses with numbers — both pass (L1).
- Each of L1–L6 has an AC + oracle; `failure_is_cheap` is measured, not asserted.
- Retrieval is fully bound (frozen `embedding`/`retrieval` blocks + `search_claim`);
  novelty is baseline-subtraction; both recalls are reported.
- The runner-cell needs no persistent box-agent and no always-on ω.

## What this deliberately does NOT establish (known debt)
- That non-retrieved relations do not exist (heuristic search cannot prove absence).
- That any candidate is an *identified relation* or a *tradeable incoherence* —
  that is downstream (classification → atlas → law → price LP → witness).
- Exact-retrieval feasibility at full N (bound by the probe; exact mode is refused
  if quadratic wall-clock is projected).
