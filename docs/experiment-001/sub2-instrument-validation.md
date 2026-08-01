# Sub 2 — joint-state classifier instrument validation (dry run)

Status: **instrument validation only** — 5 hand-built representative pairs, NOT
drawn from the frozen corpus. It proves the semantic instrument end-to-end while
cn-sigma was offline; it is not a corpus result and not an economic edge.

Fresh blind subagent (saw only `classification_input`, no ground truth, no prices)
→ receipts in `instrument_validation_receipts.jsonl`. Ground truth joined afterward.

| pair | kind | expected | classifier result | correct? |
|---|---|---|---|---|
| threshold ladder (BTC ≥150k vs ≥100k) | A | a_implies_b | `a_implies_b`, supported_candidate | ✓ |
| single-winner (Trump vs Harris) | A | mutually_exclusive | `mutually_exclusive`; refused JE (00 possible: 3rd candidate) | ✓ |
| ceasefire vs war-ends | C | b_implies_a? | **unresolved** — 2 retained interpretations, I2 gives 01 counterexample | ✓ (honest) |
| Lakers vs BTC | D | unrelated | rejected (all 4 states possible) | ✓ |
| Man City EPL vs UCL | C | correlated_only | rejected — no logical edge (correlated ≠ arbitrage) | ✓ |

Metrics (dry run): stratum-A known-positive recall 2/2; 4 counterexamples recorded;
correlated/unrelated correctly rejected; ceasefire correctly unresolved.

What it validates: per-interpretation 2×2 matrices, mechanical property derivation,
`robust_derived_properties` only across retained interpretations, counterexample
refinement, ME-without-JE discrimination, heuristic ⇒ supported_candidate /
not_established. The corpus pilot (#3) reuses this exact harness on the real
seeded-20 batch once cn-sigma is back.
