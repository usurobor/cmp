#!/usr/bin/env python3
"""build_review_batch.py — blinded, stratified pilot batch for Sub 2 (#3).

Emits two artifacts (deterministic, seeded):
  derived/classification_input.jsonl   what the classifier sees (allowlist ONLY)
  derived/evaluation_manifest.jsonl    ground truth, joined afterward

Strata:
  A hidden known positives (RELATION-SPECIFIC):
      - exact-one basket (negRisk event, >=2 members): every member pair -> mutually_exclusive;
        jointly_exhaustive ONLY when the basket has exactly 2 members.
      - threshold ladder: identical question-sans-number + same resolution_source + same end_date
        + upward comparator, different thresholds -> a_implies_b (higher threshold implies lower).
      - (titles that merely look similar are NOT auto-equivalent.)
  B cross-event semantic candidates (from candidate_pairs.jsonl, same_event=false)
  C hard near-miss negatives (shared actors but different resolution_source or end_date)
  D unrelated controls (random pairs, different event, no shared entity)

Blinding: classification_input carries only contract text + ids + version digests + honest
time fields. NO stratum, expected relation, retrieval reason/score, price, or outcome.
Outcome-blind: price/outcome/umaResolutionStatus fields are never read into the input.

Corpus: $CMP_DATA_DIR/derived/liquid_volume_ge_10000.jsonl
Pairs:  $GITHUB_WORKSPACE/derived/candidate_pairs.jsonl  (from gen_candidate_pairs.py)
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
from collections import defaultdict
from datetime import datetime, timezone

DATA_DIR = os.environ.get("CMP_DATA_DIR", "/var/lib/cmp/data")
JSONL = os.path.join(DATA_DIR, "derived", "liquid_volume_ge_10000.jsonl")
WS = os.environ.get("GITHUB_WORKSPACE", ".")
PAIRS = os.path.join(WS, "derived", "candidate_pairs.jsonl")
OUT_DIR = os.path.join(WS, "derived")
INPUT = os.path.join(OUT_DIR, "classification_input.jsonl")
MANIFEST = os.path.join(OUT_DIR, "evaluation_manifest.jsonl")

SEED = int(os.environ.get("BATCH_SEED", "20260801"))
PER = {"A": 6, "B": 6, "C": 5, "D": 3}   # total 20

NUM_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?")
UP = re.compile(r"\b(above|over|greater|exceed|at least|>=|≥|more than|reach)\b", re.I)
ENT_RE = re.compile(r"\b([A-Z][a-zA-Z0-9.&'-]+(?:\s+[A-Z][a-zA-Z0-9.&'-]+){0,3})\b")


def digest(*parts) -> str:
    return hashlib.sha256("".join(str(p or "") for p in parts).encode()).hexdigest()[:16]


def norm_no_num(q: str) -> str:
    return NUM_RE.sub("#", (q or "").lower()).strip()


def ents(text: str) -> set:
    return {e.lower() for e in ENT_RE.findall(text or "") if len(e) >= 3}


def main() -> None:
    rng = random.Random(SEED)
    # pass 1: light per-market records for positives/controls (no full text retained)
    events = defaultdict(list)          # event_id -> [(id, negRisk)]
    ladder = defaultdict(list)          # (qkey, source, end) -> [(id, maxnum, up)]
    light = {}                          # id -> {q, ent, event, source, end}
    with open(JSONL) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            toks = m.get("clobTokenIds")
            if not toks:
                continue
            mid = m.get("id")
            q = m.get("question") or ""
            src = (m.get("resolutionSource") or "").strip().lower()
            end = (m.get("endDate") or "")[:10]
            ev = m.get("events")
            eid = ev[0]["id"] if isinstance(ev, list) and ev and isinstance(ev[0], dict) else m.get("eventId")
            light[mid] = {"q": q, "ent": ents(q), "event": eid, "source": src, "end": end}
            if eid:
                events[eid].append((mid, bool(m.get("negRisk"))))
            nums = [float(n.replace(",", "").replace("$", "")) for n in NUM_RE.findall(q)
                    if n.replace(",", "").replace("$", "").replace(".", "").isdigit()]
            if nums and UP.search(q):
                ladder[(norm_no_num(q), src, end)].append((mid, max(nums), True))

    # ---- stratum A: relation-specific known positives ----
    posA = []
    for eid, members in events.items():
        ms = [mid for mid, nr in members if nr]
        if len(ms) >= 2:
            je = len(ms) == 2
            for i in range(len(ms)):
                for j in range(i + 1, len(ms)):
                    rel = "mutually_exclusive+jointly_exhaustive" if je else "mutually_exclusive"
                    posA.append((ms[i], ms[j], rel, "exact_one_basket"))
    for key, rows in ladder.items():
        if len(rows) >= 2:
            rows = sorted(rows, key=lambda r: r[1])
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    if rows[i][1] != rows[j][1]:
                        # higher threshold implies lower (both upward comparator, same q/source/end)
                        posA.append((rows[j][0], rows[i][0], "a_implies_b", "threshold_ladder"))

    # ---- stratum B: cross-event candidates from the generator ----
    candB = []
    if os.path.exists(PAIRS):
        with open(PAIRS) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not r.get("same_event"):
                    candB.append((r["a"], r["b"], r.get("candidate_reasons", []), r.get("score")))

    # ---- stratum C: hard near-miss negatives (shared actors, different source/end) ----
    ent_idx = defaultdict(list)
    for mid, f in light.items():
        for e in list(f["ent"])[:6]:
            ent_idx[e].append(mid)
    candC = []
    for e, ids in ent_idx.items():
        if 2 <= len(ids) <= 400:
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    a, b = ids[i], ids[j]
                    fa, fb = light[a], light[b]
                    shared = len(fa["ent"] & fb["ent"])
                    if shared >= 2 and (fa["source"] != fb["source"] or fa["end"] != fb["end"]):
                        candC.append((a, b))
        if len(candC) > 5000:
            break

    # ---- stratum D: unrelated controls (random, different event, no shared entity) ----
    ids_all = list(light.keys())
    candD = []
    tries = 0
    while len(candD) < 200 and tries < 20000:
        tries += 1
        a, b = rng.choice(ids_all), rng.choice(ids_all)
        if a == b:
            continue
        fa, fb = light[a], light[b]
        if fa["event"] and fa["event"] == fb["event"]:
            continue
        if fa["ent"] & fb["ent"]:
            continue
        candD.append((a, b))

    def pick(cands, n):
        uniq, seen = [], set()
        for c in cands:
            key = tuple(sorted((c[0], c[1])))
            if key in seen:
                continue
            seen.add(key)
            uniq.append(c)
        rng.shuffle(uniq)
        return uniq[:n]

    chosen = []
    for a, b, rel, kind in pick(posA, PER["A"]):
        chosen.append((a, b, "A", rel, kind, ["mechanical_positive"], None))
    for a, b, reasons, score in pick(candB, PER["B"]):
        chosen.append((a, b, "B", "unknown", "candidate", reasons, score))
    for a, b in pick(candC, PER["C"]):
        chosen.append((a, b, "C", "expected_negative_or_unresolved", "near_miss", ["same_actors"], None))
    for a, b in pick(candD, PER["D"]):
        chosen.append((a, b, "D", "unrelated", "control", [], None))

    # pass 2: pull full text only for chosen ids (blinding-safe allowlist)
    want = {i for row in chosen for i in (row[0], row[1])}
    full = {}
    with open(JSONL) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = m.get("id")
            if mid in want:
                desc = m.get("description") or ""
                void = " ".join(s for s in re.split(r"(?<=[.])\s+", desc)
                                if re.search(r"\bvoid|cancel|n/?a|refund|invalid\b", s, re.I))[:600]
                full[mid] = {"question": m.get("question") or "",
                             "full_rules": desc[:6000],
                             "resolution_source": m.get("resolutionSource") or "",
                             "end_date": m.get("endDate"),
                             "void_conditions": void or None,
                             "digest": digest(m.get("question"), desc, m.get("resolutionSource"),
                                              m.get("endDate"))}

    observed = datetime.now(timezone.utc).isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(INPUT, "w") as fin, open(MANIFEST, "w") as fman:
        for a, b, stratum, rel, kind, reasons, score in chosen:
            fa, fb = full.get(a, {}), full.get(b, {})
            fin.write(json.dumps({
                "a_id": a, "b_id": b,
                "contract_a": {k: fa.get(k) for k in
                               ("question", "full_rules", "resolution_source", "end_date", "void_conditions")},
                "contract_b": {k: fb.get(k) for k in
                               ("question", "full_rules", "resolution_source", "end_date", "void_conditions")},
                "a_version_digest": fa.get("digest"), "b_version_digest": fb.get("digest"),
                "text_observed_at": observed, "version_basis": "final_api_snapshot",
                "historical_contract_version": "unavailable",
                "historical_discoverability": "not_established"}) + "\n")
            fman.write(json.dumps({
                "a_id": a, "b_id": b, "stratum": stratum, "expected_relation": rel,
                "known_positive_type": kind, "retrieval_channel": reasons, "score": score}) + "\n")

    print(f"batch: {len(chosen)} pairs  (A={PER['A']} B={PER['B']} C={PER['C']} D={PER['D']}, seed={SEED})")
    print(f"pools: posA={len(posA)} candB={len(candB)} candC={len(candC)} candD={len(candD)}")
    print(f"input    -> {INPUT}")
    print(f"manifest -> {MANIFEST}")
    print("classification_input is blinded: no stratum, expected relation, score, price, or outcome.")


if __name__ == "__main__":
    main()
