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
