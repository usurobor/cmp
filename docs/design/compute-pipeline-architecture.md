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
