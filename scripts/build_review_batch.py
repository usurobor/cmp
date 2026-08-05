#!/usr/bin/env python3
"""build_review_batch.py — blinded, stratified pilot batch for Sub 2 (#3).

MEMORY-BOUNDED (v2): the earlier version held all ~357k markets (with entity
sets) in RAM and depended on gen_candidate_pairs (which materializes millions of
candidate pairs) — the same unbounded pattern that OOM-killed the collector.
This version keeps RSS ~O(sample):
  - stratum A (known positives) is grounded in the FULL corpus but only via small
    streaming structures (event groups + threshold-ladder buckets, tiny tuples);
  - strata B/C/D are drawn from a seeded RESERVOIR SAMPLE of SAMPLE_N markets, so
    the near-miss/candidate/control pools never require the whole corpus in RAM.
No dependency on gen_candidate_pairs.

Emits (deterministic, seeded):
  derived/classification_input.jsonl   allowlist ONLY (blinded, outcome-blind)
  derived/evaluation_manifest.jsonl    ground truth (joined afterward)

Corpus: $CMP_DATA_DIR/derived/liquid_volume_ge_10000.jsonl
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
OUT_DIR = os.path.join(os.environ.get("GITHUB_WORKSPACE", "."), "derived")
INPUT = os.path.join(OUT_DIR, "classification_input.jsonl")
MANIFEST = os.path.join(OUT_DIR, "evaluation_manifest.jsonl")

SEED = int(os.environ.get("BATCH_SEED", "20260801"))
SAMPLE_N = int(os.environ.get("BATCH_SAMPLE", "12000"))   # reservoir for B/C/D
PER = {"A": 6, "B": 6, "C": 5, "D": 3}

NUM_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?")
UP = re.compile(r"\b(above|over|greater|exceed|at least|>=|≥|more than|reach)\b", re.I)
ENT_RE = re.compile(r"\b([A-Z][a-zA-Z0-9.&'-]+(?:\s+[A-Z][a-zA-Z0-9.&'-]+){0,3})\b")
STOP_ENT = {"will", "the", "yes", "no", "us", "u.s", "u.s.", "usa"} | {str(y) for y in range(2018, 2031)}


def digest(*p) -> str:
    return hashlib.sha256("".join(str(x or "") for x in p).encode()).hexdigest()[:16]


def norm_no_num(q: str) -> str:
    return NUM_RE.sub("#", (q or "").lower()).strip()


def ents(text: str) -> set:
    out = set()
    for e in ENT_RE.findall(text or ""):
        low = e.lower()
        if low not in STOP_ENT and len(low) >= 3:
            out.add(low)
    return set(list(out)[:8])


def main() -> None:
    if not os.path.exists(JSONL):
        raise SystemExit(f"no liquid corpus at {JSONL}")
    rng = random.Random(SEED)

    events = defaultdict(list)     # event_id -> [(id, negRisk)]   (small tuples)
    ladder = defaultdict(list)     # (qkey, source, end) -> [(id, maxnum)]
    sample = []                    # reservoir of <=SAMPLE_N minimal records
    n_seen = 0

    with open(JSONL) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not m.get("clobTokenIds"):
                continue
            mid = m.get("id")
            q = m.get("question") or ""
            src = (m.get("resolutionSource") or "").strip().lower()
            end = (m.get("endDate") or "")[:10]
            ev = m.get("events")
            eid = ev[0]["id"] if isinstance(ev, list) and ev and isinstance(ev[0], dict) else m.get("eventId")
            # --- full-corpus, bounded structures for stratum A ---
            if eid:
                events[eid].append((mid, bool(m.get("negRisk"))))
            nums = [float(x.replace(",", "").replace("$", "")) for x in NUM_RE.findall(q)
                    if x.replace(",", "").replace("$", "").replace(".", "").isdigit()]
            if nums and UP.search(q):
                ladder[(norm_no_num(q), src, end)].append((mid, max(nums)))
            # --- reservoir sample for B/C/D ---
            n_seen += 1
            rec = None
            if len(sample) < SAMPLE_N:
                rec = (mid, q, src, end, eid)
                sample.append(rec)
            else:
                j = rng.randint(0, n_seen - 1)
                if j < SAMPLE_N:
                    sample[j] = (mid, q, src, end, eid)

    # entities only for the bounded sample
    sfeat = {r[0]: {"q": r[1], "src": r[2], "end": r[3], "event": r[4], "ent": ents(r[1])}
             for r in sample}
    sids = list(sfeat)

    # ---- stratum A: relation-specific known positives (full corpus) ----
    posA = []
    for eid, members in events.items():
        ms = [mid for mid, nr in members if nr]
        if len(ms) >= 2:
            je = len(ms) == 2
            for i in range(min(len(ms), 12)):
                for k in range(i + 1, min(len(ms), 12)):
                    rel = "mutually_exclusive+jointly_exhaustive" if je else "mutually_exclusive"
                    posA.append((ms[i], ms[k], rel, "exact_one_basket"))
    for _, rows in ladder.items():
        if len(rows) >= 2:
            rows = sorted(set(rows), key=lambda r: r[1])
            for i in range(len(rows)):
                for k in range(i + 1, len(rows)):
                    if rows[i][1] != rows[k][1]:
                        posA.append((rows[k][0], rows[i][0], "a_implies_b", "threshold_ladder"))

    # ---- strata B/C/D from the bounded sample via a small entity index ----
    idx = defaultdict(list)
    for mid, f in sfeat.items():
        for e in f["ent"]:
            idx[e].append(mid)
    candB, candC = [], []
    seen_pair = set()
    for e, ids in idx.items():
        if len(ids) > 300 or len(ids) < 2:
            continue
        for i in range(len(ids)):
            for k in range(i + 1, len(ids)):
                a, b = ids[i], ids[k]
                key = tuple(sorted((a, b)))
                if key in seen_pair:
                    continue
                seen_pair.add(key)
                fa, fb = sfeat[a], sfeat[b]
                if len(fa["ent"] & fb["ent"]) < 2:
                    continue
                cross = not (fa["event"] and fa["event"] == fb["event"])
                if cross:
                    candB.append((a, b))
                if fa["src"] != fb["src"] or fa["end"] != fb["end"]:
                    candC.append((a, b))
    candD = []
    tries = 0
    while len(candD) < 200 and tries < 20000:
        tries += 1
        a, b = rng.choice(sids), rng.choice(sids)
        if a == b:
            continue
        fa, fb = sfeat[a], sfeat[b]
        if (fa["event"] and fa["event"] == fb["event"]) or (fa["ent"] & fb["ent"]):
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
        chosen.append((a, b, "A", rel, kind, ["mechanical_positive"]))
    for a, b in pick(candB, PER["B"]):
        chosen.append((a, b, "B", "unknown", "candidate", ["same_actors_cross_event"]))
    for a, b in pick(candC, PER["C"]):
        chosen.append((a, b, "C", "expected_negative_or_unresolved", "near_miss", ["same_actors_diff_source_or_date"]))
    for a, b in pick(candD, PER["D"]):
        chosen.append((a, b, "D", "unrelated", "control", []))

    # ---- pass 2: full text ONLY for chosen ids (bounded) ----
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
                                if re.search(r"void|cancel|n/?a|refund|invalid", s, re.I))[:600]
                full[mid] = {"question": m.get("question") or "", "full_rules": desc[:6000],
                             "resolution_source": m.get("resolutionSource") or "",
                             "end_date": m.get("endDate"), "void_conditions": void or None,
                             "digest": digest(m.get("question"), desc, m.get("resolutionSource"), m.get("endDate"))}

    observed = datetime.now(timezone.utc).isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(INPUT, "w") as fin, open(MANIFEST, "w") as fman:
        for a, b, stratum, rel, kind, reasons in chosen:
            fa, fb = full.get(a, {}), full.get(b, {})
            fin.write(json.dumps({
                "a_id": a, "b_id": b,
                "contract_a": {k: fa.get(k) for k in ("question", "full_rules", "resolution_source", "end_date", "void_conditions")},
                "contract_b": {k: fb.get(k) for k in ("question", "full_rules", "resolution_source", "end_date", "void_conditions")},
                "a_version_digest": fa.get("digest"), "b_version_digest": fb.get("digest"),
                "text_observed_at": observed, "version_basis": "final_api_snapshot",
                "historical_contract_version": "unavailable", "historical_discoverability": "not_established"}) + "\n")
            fman.write(json.dumps({"a_id": a, "b_id": b, "stratum": stratum, "expected_relation": rel,
                                   "known_positive_type": kind, "retrieval_channel": reasons}) + "\n")

    print(f"seen={n_seen} sample={len(sample)}  chosen={len(chosen)} "
          f"(A={sum(1 for c in chosen if c[2]=='A')} B={sum(1 for c in chosen if c[2]=='B')} "
          f"C={sum(1 for c in chosen if c[2]=='C')} D={sum(1 for c in chosen if c[2]=='D')})")
    print(f"pools: posA={len(posA)} candB={len(candB)} candC={len(candC)} candD={len(candD)}")
    print(f"input -> {INPUT}\nmanifest -> {MANIFEST}")


if __name__ == "__main__":
    main()
