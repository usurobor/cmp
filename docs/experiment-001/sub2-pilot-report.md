# Sub 2 — pairwise semantic-relation pilot (real corpus, 20 pairs)

Status: **first real corpus result.** Discovery census only (final-text; `historical_discoverability: not_established`). No prices, SAT/SMT, or LP. Heuristic pass ⇒ every relation is `supported_candidate` / `identification_status: not_established`, never `identified`.

Pipeline: `derived/liquid_volume_ge_10000.jsonl` (357,664) → memory-bounded stratified batch (seed 20260801) → blinded `classification_input.jsonl` (20 pairs) → **fresh blind subagent** (saw only the input; no manifest, labels, or prices) → `candidate_relations.jsonl` → joined to `evaluation_manifest.jsonl`.

## Metrics

| stratum | n | outcome |
|---|---|---|
| A — hidden known positives | 6 | **6/6 recovered** with the correct derived property |
| B — cross-event candidates | 6 | 2 `rejected` (independent), 4 `unresolved` (non-binary) |
| C — hard near-miss | 5 | 5 `unresolved` (non-binary / different event) |
| D — unrelated controls | 3 | 3 `unresolved` (non-binary) |

Counterexamples recorded: 2. **False logical edges emitted: 0.**

## Three findings

**1. The classifier instrument works on real data.** All 6 mechanical positives were recovered blind, with witnesses and exact clauses:
- 4 crypto **threshold ladders** (same date/candle/source, e.g. ETH-High ≥2600 vs ≥2500; BTC-Close >96k vs >94k) → `a_implies_b`.
- a football **win-vs-draw** (same match) and an Elon **tweet-count bucket** pair (90–104 vs 105–119) → `mutually_exclusive`, and it correctly **refused `jointly_exhaustive`** (00 reachable).
Two both-binary but logically independent pairs (different candles / different events) were correctly `rejected` — no fabricated relation. Zero false edges.

**2. Central-hypothesis signal — UNDERDETERMINED, not negative.** The cross-event strata (B/C) surfaced **no new logical relation**. The only relations found were the same-event mechanical positives (A) — i.e. relations the mechanical signals already imply. The cross-event candidates were mostly same-*template*, different-*event* pairs (two different BTTS matches; two different CS maps) — topically similar, logically independent. **Caveat (TSC incomplete-search ≠ absence):** this is 20 pairs, one seed, ONE retrieval channel (shared-entity + structuring signal), no embeddings, ~0.006% of the corpus. It shows *this channel at this scale* did not beat mechanical signals; it is **not** evidence that latent cross-event relations are absent. Standing: `underdetermined`.

**3. Domain finding (material): the 50-50 void clause.** ~12/20 pairs are `unresolved` because at least one contract is **not genuinely binary** — most Polymarket sports/esports markets carry a *"canceled/tie → resolve 50-50"* clause (`binary_with_void_or_refund`), and temperature/price-range/vote-count markets are `multi_outcome`/`scalar`. Only ~8/20 pairs were clean `binary/binary`. The classifier correctly declined to coerce the rest into {0,1}. **Implication:** the clean-binary logical-arbitrage universe is a *sub*-universe; the sports/esports bulk needs the payoff-vector representation `f_i(s)∈[0,1]`, not the 2×2. The richest clean-binary logical structure sits in crypto threshold ladders and single-winner/threshold baskets.

## What this does and does not establish

- ✅ The α/β instrument (per-interpretation joint-state matrices, mechanical property derivation, robust-across-interpretations, counterexample refinement, settlement-domain gate) works on real corpus contracts.
- ✅ Relation-specific mechanical positives are 100% recoverable and give a trustworthy calibration floor.
- ❓ Whether semantic search recovers **non-obvious cross-event** relations the exchange did not group — **unproven**; the mechanical channel did not, and the discriminating test (embeddings retrieval + clean-binary targeting) has not been run.
- 🚫 No economic edge is claimed; prices were never consulted.

## Next slices (in order)

1. **Upgrade retrieval before re-judging the hypothesis:** embedding retrieval over full rules + target clean-binary domains (crypto thresholds/deadlines, elections, macro), and re-measure known-positive recall + cross-event yield. The pilot proved the classifier; the *retrieval* half is the open question.
2. **Payoff-vector settlement model** `f_i(s)∈[0,1]` so `binary_with_void_or_refund` / `multi_outcome` markets (the corpus majority) become analyzable instead of `unresolved`.
3. Only then: freeze validated components → selective price overlay (`Q_t ∩ conv(S_C)`) → witness. Still no SAT/LP until multi-node components exist.

Artifacts: `derived/candidate_relations.jsonl` (receipts), `derived/classification_input.jsonl` (blinded input), `derived/evaluation_manifest.jsonl` (ground truth), `scripts/join_metrics.py`.
