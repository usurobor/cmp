#!/usr/bin/env python3
"""join_metrics.py — join classifier receipts to the evaluation manifest and score.

Reads:
  candidate_relations.jsonl   receipts from the (blind) classifier
  evaluation_manifest.jsonl   ground truth (stratum, expected_relation, channel)
Writes a metrics summary per retrieval channel / stratum:
  candidate yield, relation precision, known-positive recall, unresolved rate,
  counterexample overturn rate.

Honest caveats printed: known-positive recall shares signals with the retrieval
channels; a heuristic pass yields supported_candidate, not identified.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict


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


def main() -> None:
    receipts = load(sys.argv[1] if len(sys.argv) > 1 else "derived/candidate_relations.jsonl")
    manifest = load(sys.argv[2] if len(sys.argv) > 2 else "derived/evaluation_manifest.jsonl")

    by_stratum = defaultdict(lambda: defaultdict(int))
    overturns = 0
    total = 0
    for key, gt in manifest.items():
        s = gt.get("stratum", "?")
        by_stratum[s]["n"] += 1
        total += 1
        rec = receipts.get(key)
        if not rec:
            by_stratum[s]["missing"] += 1
            continue
        disp = rec.get("relation_disposition")
        props = set(rec.get("robust_derived_properties") or [])
        if disp == "unresolved":
            by_stratum[s]["unresolved"] += 1
        if rec.get("counterexamples"):
            overturns += 1
        expected = gt.get("expected_relation", "")
        # relation precision: did a derived property match the expected relation?
        exp_props = {p.strip() for p in expected.replace("+", " ").split() if "_" in p}
        if exp_props and (props & exp_props):
            by_stratum[s]["match"] += 1
        elif disp == "rejected" and s in ("C", "D"):
            by_stratum[s]["correct_reject"] += 1

    print("===== PILOT METRICS =====")
    print(f"pairs: {total}  receipts: {len(receipts)}  overturns(counterexamples): {overturns}")
    print(f"{'stratum':<8}{'n':>3}{'match':>7}{'correct_rej':>13}{'unresolved':>12}{'missing':>9}")
    for s in sorted(by_stratum):
        d = by_stratum[s]
        print(f"{s:<8}{d['n']:>3}{d.get('match',0):>7}{d.get('correct_reject',0):>13}"
              f"{d.get('unresolved',0):>12}{d.get('missing',0):>9}")
    # headline rates
    A = by_stratum.get("A", {})
    if A.get("n"):
        print(f"\nknown-positive recall (stratum A): {A.get('match',0)}/{A['n']} "
              f"(caveat: positives share signals with retrieval channels)")
    print("NOTE: heuristic pass -> relation_disposition=supported_candidate, "
          "identification_status=not_established. Not an economic edge.")


if __name__ == "__main__":
    main()
