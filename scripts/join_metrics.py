#!/usr/bin/env python3
"""join_metrics.py — join classifier receipts to the evaluation manifest and score.

Reads:
  candidate_relations.jsonl   receipts from the (blind) classifier
  evaluation_manifest.jsonl   ground truth (stratum, expected_relation, channel)

Manifest-aware: handles both the pilot manifest (strata A/B/C/D, explicit
expected_relation like "a_implies_b") and the embedding-slice manifest
(strata S_high/S_mid/S_low = semantic-only cross-event candidates with
expected_relation "unknown"; INJ_pos = injected known positives).

Headline for the embedding slice: how many MECHANICALLY-INVISIBLE cross-event
pairs yielded a logical relation. That count is the hypothesis signal the pilot
could not produce, so any such pair is listed in full.

Honest caveats printed: a heuristic pass yields supported_candidate, never
identified; known-positive recall shares signals with the retrieval channel.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict

LOGICAL = {"a_implies_b", "b_implies_a", "equivalent",
           "mutually_exclusive", "jointly_exhaustive"}


def load(path):
    out = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            out[tuple(sorted((str(r.get("a_id")), str(r.get("b_id")))))] = r
    return out


def expected_props(expected: str) -> set:
    # tokens carrying an underscore are property names (a_implies_b, ...);
    # "a_implies_b_or_ladder" is a loose INJ label -> map to the ladder family.
    if "or_ladder" in expected:
        return {"a_implies_b", "b_implies_a", "equivalent"}
    return {p.strip() for p in expected.replace("+", " ").split() if "_" in p}


def main() -> None:
    receipts = load(sys.argv[1] if len(sys.argv) > 1 else "derived/candidate_relations.jsonl")
    manifest = load(sys.argv[2] if len(sys.argv) > 2 else "derived/evaluation_manifest.jsonl")

    by = defaultdict(lambda: defaultdict(int))
    overturns = 0
    total = 0
    semantic_hits = []   # the meaningful output: relations found in S_* strata
    inj = {"n": 0, "recovered": 0}

    for key, gt in manifest.items():
        s = gt.get("stratum", "?")
        by[s]["n"] += 1
        total += 1
        rec = receipts.get(key)
        if not rec:
            by[s]["missing"] += 1
            continue
        disp = rec.get("relation_disposition")
        props = set(rec.get("robust_derived_properties") or [])
        logical = props & LOGICAL
        if rec.get("counterexamples"):
            overturns += 1
        if disp == "unresolved":
            by[s]["unresolved"] += 1
        elif disp == "rejected":
            by[s]["rejected"] += 1
        if logical:
            by[s]["found"] += 1

        exp = expected_props(gt.get("expected_relation", ""))

        if s == "INJ_pos":
            inj["n"] += 1
            if logical:
                inj["recovered"] += 1
        elif s.startswith("S_"):
            # semantic-only cross-event pair with a found logical relation ==
            # exactly what the pilot's mechanical channel could not surface.
            if logical:
                semantic_hits.append((key, sorted(logical), disp, rec))
        else:
            # pilot-style strata: score against explicit expected props
            if exp and (props & exp):
                by[s]["match"] += 1
            elif disp == "rejected" and s in ("C", "D"):
                by[s]["correct_reject"] += 1

    print("===== METRICS =====")
    print(f"pairs: {total}  receipts: {len(receipts)}  overturns(counterexamples): {overturns}")
    hdr = f"{'stratum':<10}{'n':>3}{'found':>7}{'match':>7}{'corr_rej':>10}{'unres':>7}{'rej':>5}{'miss':>6}"
    print(hdr)
    for s in sorted(by):
        d = by[s]
        print(f"{s:<10}{d['n']:>3}{d.get('found',0):>7}{d.get('match',0):>7}"
              f"{d.get('correct_reject',0):>10}{d.get('unresolved',0):>7}"
              f"{d.get('rejected',0):>5}{d.get('missing',0):>6}")

    if inj["n"]:
        print(f"\ninjected known-positive recall (INJ_pos): {inj['recovered']}/{inj['n']} "
              f"(caveat: injected positives share the threshold-ladder signal)")
    A = by.get("A", {})
    if A.get("n"):
        print(f"known-positive recall (stratum A): {A.get('match',0)}/{A['n']} "
              f"(caveat: positives share signals with retrieval channels)")

    # ---- the headline: semantic-only cross-event relations found ----
    sem_n = sum(by[s]["n"] for s in by if s.startswith("S_"))
    if sem_n:
        print(f"\n===== HYPOTHESIS SIGNAL =====")
        print(f"semantic-only (mechanically-invisible) cross-event pairs judged: {sem_n}")
        print(f"of those, a logical relation was FOUND in: {len(semantic_hits)}")
        if semantic_hits:
            print("  ↳ these are candidate relations the mechanical channel could not reach:")
            for key, logical, disp, rec in semantic_hits:
                md = rec.get("material_differences") or rec.get("counterexamples") or []
                print(f"   - {key[0]} × {key[1]}: {logical}  [{disp}]")
                if md:
                    print(f"       note: {md[0] if isinstance(md, list) else md}")
        else:
            print("  ↳ none. This channel/scale surfaced no cross-event logical relation.")
            print("     (TSC: incomplete-search != absence — one embedder, one K, one seed.)")

    print("\nNOTE: heuristic pass -> relation_disposition=supported_candidate, "
          "identification_status=not_established. No prices consulted. Not an economic edge.")


if __name__ == "__main__":
    main()
