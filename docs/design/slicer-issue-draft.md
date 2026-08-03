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
