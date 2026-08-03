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
