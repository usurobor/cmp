#!/usr/bin/env python3
"""gen_candidate_pairs.py — mechanical neighbor generation for CMP (no prices).

Front of the real CMP pipeline: text -> candidate related pairs -> (classifier) ->
candidate_relations.jsonl -> constraint graph -> selective price overlay.

This step is DETERMINISTIC and PRICE-FREE. It reduces ~357k liquid contracts to a
bounded set of plausibly-related pairs using mechanical signals only -- shared
entities, shared numeric thresholds, shared years, shared resolution source. It
does NOT classify the relation (that's the Claude counterexample-search step) and
it does NOT look at prices. Cross-event pairs are the prize: relationships the
exchange did NOT already group (implications, partitions, equivalences spanning
event pages). Same-event pairs are emitted too but flagged as the easy control.

Blocking via inverted index (entity -> contracts) keeps it out of O(n^2); huge
"stopword" buckets are skipped; neighbors per contract are capped.

Corpus: $CMP_DATA_DIR/derived/liquid_volume_ge_10000.jsonl
Out:    ./derived/candidate_pairs.jsonl  (workspace; box /var/lib is not writable by CI)
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict

DATA_DIR = os.environ.get("CMP_DATA_DIR", "/var/lib/cmp/data")
JSONL = os.path.join(DATA_DIR, "derived", "liquid_volume_ge_10000.jsonl")
OUT_DIR = os.path.join(os.environ.get("GITHUB_WORKSPACE", "."), "derived")
OUT = os.path.join(OUT_DIR, "candidate_pairs.jsonl")

MAX_BUCKET = int(os.environ.get("PAIR_MAX_BUCKET", "1500"))   # skip stopword-ish entities
NEIGHBORS = int(os.environ.get("PAIR_NEIGHBORS", "25"))       # cap per contract
MIN_SCORE = float(os.environ.get("PAIR_MIN_SCORE", "2"))      # >=2 shared signals

STOP = {"will", "the", "a", "an", "yes", "no", "by", "in", "on", "of", "to", "and",
        "or", "be", "is", "are", "at", "for", "win", "wins", "reach", "above",
        "below", "before", "after", "us", "u.s", "u.s.", "usa", "market", "markets",
        "election", "2024", "2025", "2026"}

ENTITY_RE = re.compile(r"\b([A-Z][a-zA-Z0-9.&'-]+(?:\s+[A-Z][a-zA-Z0-9.&'-]+){0,3})\b")
NUM_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s?(?:%|percent|bps|k|m|bn|billion|million|thousand)?", re.I)
YEAR_RE = re.compile(r"\b20\d{2}\b")


def _plist(x):
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            return json.loads(x)
        except json.JSONDecodeError:
            return None
    return None


def features(m: dict) -> dict:
    q = m.get("question") or m.get("groupItemTitle") or ""
    text = " ".join(str(m.get(k) or "") for k in ("question", "description", "resolutionSource"))
    ents = set()
    for e in ENTITY_RE.findall(text):
        e = e.strip()
        low = e.lower()
        if low in STOP or len(low) < 3:
            continue
        ents.add(low)
    nums = set()
    for n in NUM_RE.findall(text):
        n = n.strip().lower().replace(",", "").replace("$", "")
        if n and any(c.isdigit() for c in n) and not YEAR_RE.fullmatch(n):
            nums.add(n)
    years = set(YEAR_RE.findall(text))
    ev = m.get("events")
    event = ev[0]["id"] if isinstance(ev, list) and ev and isinstance(ev[0], dict) else m.get("eventId")
    return {"id": m.get("id"), "q": q[:120], "ents": ents, "nums": nums,
            "years": years, "event": event,
            "src": (m.get("resolutionSource") or "").strip().lower()[:60]}


def main() -> None:
    if not os.path.exists(JSONL):
        sys.exit(f"no liquid corpus at {JSONL}")
    feats: list[dict] = []
    ent_index: dict[str, list[int]] = defaultdict(list)
    with open(JSONL) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not _plist(m.get("clobTokenIds")):
                continue
            f = features(m)
            idx = len(feats)
            feats.append(f)
            for e in f["ents"]:
                ent_index[e].append(idx)
    print(f"contracts: {len(feats)} | distinct entities: {len(ent_index)}")

    # candidate neighbors via shared entities (skip stopword-ish giant buckets)
    scored: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for e, idxs in ent_index.items():
        if len(idxs) < 2 or len(idxs) > MAX_BUCKET:
            continue
        w = 1.0 + 1.0 / len(idxs)  # rarer entity -> slightly stronger signal
        for a in range(len(idxs)):
            ia = idxs[a]
            for b in range(a + 1, len(idxs)):
                scored[ia][idxs[b]] += w
                scored[idxs[b]][ia] += w

    os.makedirs(OUT_DIR, exist_ok=True)
    seen, n_pairs, n_cross = set(), 0, 0
    samples = []
    with open(OUT, "w") as out:
        for ia, nbrs in scored.items():
            fa = feats[ia]
            top = sorted(nbrs.items(), key=lambda kv: kv[1], reverse=True)[:NEIGHBORS]
            for ib, ent_score in top:
                key = (ia, ib) if ia < ib else (ib, ia)
                if key in seen:
                    continue
                seen.add(key)
                fb = feats[ib]
                shared_ents = sorted(fa["ents"] & fb["ents"])
                shared_nums = sorted(fa["nums"] & fb["nums"])
                shared_years = sorted(fa["years"] & fb["years"])
                same_src = bool(fa["src"] and fa["src"] == fb["src"])
                score = len(shared_ents) + 1.5 * len(shared_nums) + 0.5 * len(shared_years) + (1 if same_src else 0)
                if score < MIN_SCORE:
                    continue
                same_event = bool(fa["event"] and fa["event"] == fb["event"])
                rec = {"a": fa["id"], "b": fb["id"], "qa": fa["q"], "qb": fb["q"],
                       "same_event": same_event, "shared_entities": shared_ents[:8],
                       "shared_thresholds": shared_nums[:6], "shared_years": shared_years,
                       "same_source": same_src, "score": round(score, 2)}
                out.write(json.dumps(rec) + "\n")
                n_pairs += 1
                if not same_event:
                    n_cross += 1
                    if len(samples) < 25:
                        samples.append(rec)

    print(f"\n===== CANDIDATE PAIRS =====")
    print(f"total pairs: {n_pairs} | cross-event (the prize): {n_cross} | "
          f"same-event (control): {n_pairs - n_cross}")
    print(f"out -> {OUT}\n")
    print("top cross-event candidates by shared-signal score:")
    for r in sorted(samples, key=lambda r: r["score"], reverse=True):
        print(f"  score={r['score']} ents={r['shared_entities']} thr={r['shared_thresholds']}")
        print(f"     A: {r['qa']}")
        print(f"     B: {r['qb']}")
    print("\nNOTE: mechanical candidates only — NOT classified relations, NO prices. "
          "Next: Claude counterexample-search classifier -> candidate_relations.jsonl")


if __name__ == "__main__":
    main()
