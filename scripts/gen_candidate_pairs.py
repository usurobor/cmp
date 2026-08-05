#!/usr/bin/env python3
"""gen_candidate_pairs.py — structured neighbor generation for CMP (no prices).

Front of the real CMP pipeline: text -> candidate related pairs -> (Claude
counterexample classifier) -> candidate_relations.jsonl -> constraint graph ->
selective price overlay.

NOT topical clustering. Shared entities only form the *bucket* (blocking); a pair
is emitted only if it also carries a STRUCTURING signal suggesting a formal
relation, and every pair records WHY it was paired (candidate_reasons):

  near_duplicate            high full-text similarity   -> equivalence candidate
  threshold_ladder          same subject, DIFFERENT #s  -> implication candidate
  deadline_ladder           same predicate, DIFFERENT dates -> implication candidate
  parent_child              one subject subsumes other  -> partition/member
  possible_mutual_exclusion single-winner, diff winner  -> mutual exclusion

"same_actors" alone (topical) is recorded but NEVER sufficient to keep a pair.
Price-free and unclassified. Cross-event pairs are the prize (relations the
exchange did not group). Blocking via inverted index keeps it out of O(n^2).

Corpus: $CMP_DATA_DIR/derived/liquid_volume_ge_10000.jsonl
Out:    ./derived/candidate_pairs.jsonl  (workspace; box /var/lib not CI-writable)
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
REVIEW = os.path.join(OUT_DIR, "pairs_for_review.jsonl")
REVIEW_N = int(os.environ.get("PAIR_REVIEW_N", "150"))

MAX_BUCKET = int(os.environ.get("PAIR_MAX_BUCKET", "1200"))
NEIGHBORS = int(os.environ.get("PAIR_NEIGHBORS", "30"))
JACCARD_DUP = float(os.environ.get("PAIR_JACCARD", "0.6"))
TOK_CAP = 160

STOP = {"will", "the", "a", "an", "yes", "no", "by", "in", "on", "of", "to", "and",
        "or", "be", "is", "are", "at", "for", "win", "wins", "won", "reach", "above",
        "below", "before", "after", "than", "least", "more", "market", "resolve",
        "resolution", "this", "that", "with", "any", "end", "date", "how", "many",
        "which", "who", "what", "when", "during", "between"}
STOP_ENT = {"will", "the", "yes", "no", "us", "u.s", "u.s.", "usa", "trump"} | \
           {str(y) for y in range(2018, 2031)}

ENTITY_RE = re.compile(r"\b([A-Z][a-zA-Z0-9.&'-]+(?:\s+[A-Z][a-zA-Z0-9.&'-]+){0,3})\b")
WORD_RE = re.compile(r"[a-z0-9]+")
YEAR_RE = re.compile(r"\b20\d{2}\b")
# a threshold: optional $, a number, optional unit; captured as (value, scale)
THRESH_RE = re.compile(r"\$?\s?(\d[\d,]*(?:\.\d+)?)\s?(%|percent|bps|k|m|bn|billion|million|thousand)?", re.I)
MONTHS = ("january february march april may june july august september october "
          "november december").split()
PRED = {"win", "above", "below", "reach", "exceed", "hit", "least", "before", "by",
        "launch", "nominee", "nomination", "ceasefire", "agreement", "resign",
        "elected", "confirm", "approve"}
WINNER = {"win", "wins", "winner", "elected", "nominee", "champion"}


def _plist(x):
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            return json.loads(x)
        except json.JSONDecodeError:
            return None
    return None


def _norm_num(val: str, scale: str | None) -> float | None:
    try:
        v = float(val.replace(",", ""))
    except ValueError:
        return None
    s = (scale or "").lower()
    mult = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6,
            "bn": 1e9, "billion": 1e9}.get(s, 1.0)
    if s in ("%", "percent", "bps"):
        return None  # percentages/bps handled as their own comparable class below
    v *= mult
    return v if v >= 100 else None  # ignore tiny bare numbers (counts, ordinals)


def features(m: dict) -> dict:
    q = (m.get("question") or m.get("groupItemTitle") or "")
    text = " ".join(str(m.get(k) or "") for k in ("question", "description", "resolutionSource"))
    ents = set()
    for e in ENTITY_RE.findall(text):
        low = e.strip().lower()
        if low in STOP_ENT or len(low) < 3:
            continue
        ents.add(low)
    toks = [w for w in WORD_RE.findall(text.lower()) if w not in STOP and len(w) >= 3]
    tokset = frozenset(toks[:TOK_CAP])
    threshes = set()
    for val, scale in THRESH_RE.findall(text):
        n = _norm_num(val, scale)
        if n is not None and not YEAR_RE.fullmatch(val.replace(",", "")):
            threshes.add(n)
    years = set(YEAR_RE.findall(text))
    months = {mo for mo in MONTHS if mo in text.lower()}
    preds = {p for p in PRED if re.search(rf"\b{p}", text.lower())}
    ev = m.get("events")
    event = ev[0]["id"] if isinstance(ev, list) and ev and isinstance(ev[0], dict) else m.get("eventId")
    return {"id": m.get("id"), "q": q[:120], "ents": ents, "toks": tokset,
            "thr": threshes, "years": years, "months": months, "preds": preds,
            "event": event, "winner": bool(preds & WINNER),
            "src": (m.get("resolutionSource") or "").strip().lower()[:60]}


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def reasons_for(fa: dict, fb: dict) -> tuple[list[str], float]:
    shared_ents = fa["ents"] & fb["ents"]
    reasons, score = [], 0.0
    if len(shared_ents) >= 2:
        reasons.append("same_actors")  # topical only; not a keeper by itself
    jac = jaccard(fa["toks"], fb["toks"])
    if jac >= JACCARD_DUP:
        reasons.append("near_duplicate"); score += 3 + jac
    if shared_ents:
        # threshold ladder: same subject, both have thresholds, and they differ
        if fa["thr"] and fb["thr"] and fa["thr"] != fb["thr"] and (fa["thr"] ^ fb["thr"]):
            reasons.append("threshold_ladder"); score += 3
        # deadline ladder: same predicate context, differing dates
        if (fa["preds"] & fb["preds"]) and (
                (fa["years"] and fb["years"] and fa["years"] != fb["years"]) or
                (fa["months"] and fb["months"] and fa["months"] != fb["months"])):
            reasons.append("deadline_ladder"); score += 2
        # parent/child: one normalized question subsumes the other
        qa, qb = fa["q"].lower(), fb["q"].lower()
        if qa != qb and (qa in qb or qb in qa):
            reasons.append("parent_child"); score += 2
        # possible mutual exclusion: single-winner event, different winner entity
        if fa["winner"] and fb["winner"] and shared_ents and (fa["ents"] ^ fb["ents"]):
            reasons.append("possible_mutual_exclusion"); score += 1.5
    if "same_actors" in reasons:
        score += 0.5 * len(shared_ents)
    return reasons, round(score, 2)


STRUCTURING = {"near_duplicate", "threshold_ladder", "deadline_ladder",
               "parent_child", "possible_mutual_exclusion"}


def main() -> None:
    if not os.path.exists(JSONL):
        sys.exit(f"no liquid corpus at {JSONL}")
    feats, ent_index = [], defaultdict(list)
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

    # candidate pairs: co-occur in a non-giant entity bucket
    cand = defaultdict(set)
    for e, idxs in ent_index.items():
        if len(idxs) < 2 or len(idxs) > MAX_BUCKET:
            continue
        for i in range(len(idxs)):
            for j in range(i + 1, len(idxs)):
                cand[idxs[i]].add(idxs[j]); cand[idxs[j]].add(idxs[i])

    os.makedirs(OUT_DIR, exist_ok=True)
    seen, kept, cross = set(), 0, 0
    by_reason = defaultdict(int)
    samples = []
    review_rows = []
    with open(OUT, "w") as out:
        for ia, nbrs in cand.items():
            fa = feats[ia]
            scored = []
            for ib in nbrs:
                key = (ia, ib) if ia < ib else (ib, ia)
                if key in seen:
                    continue
                reasons, score = reasons_for(fa, feats[ib])
                if not (set(reasons) & STRUCTURING):   # drop pure-topical pairs
                    continue
                scored.append((score, ib, reasons))
            scored.sort(reverse=True)
            for score, ib, reasons in scored[:NEIGHBORS]:
                key = (ia, ib) if ia < ib else (ib, ia)
                if key in seen:
                    continue
                seen.add(key)
                fb = feats[ib]
                same_event = bool(fa["event"] and fa["event"] == fb["event"])
                rec = {"a": fa["id"], "b": fb["id"], "qa": fa["q"], "qb": fb["q"],
                       "candidate_reasons": reasons, "same_event": same_event,
                       "shared_entities": sorted(fa["ents"] & fb["ents"])[:6],
                       "thr_a": sorted(fa["thr"]), "thr_b": sorted(fb["thr"]),
                       "score": score}
                out.write(json.dumps(rec) + "\n")
                kept += 1
                for r in reasons:
                    by_reason[r] += 1
                if not same_event:
                    cross += 1
                    review_rows.append((score, fa["id"], fb["id"], reasons))
                    if len(samples) < 30:
                        samples.append(rec)

    # Full-text review batch for the top cross-event pairs: the price-blind input
    # to the joint-state classifier (Claude). Second pass fetches full rules text.
    review_rows.sort(reverse=True)
    top = review_rows[:REVIEW_N]
    want = {i for _, a, b, _ in top for i in (a, b)}
    fulltext = {}
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
                fulltext[mid] = {"question": m.get("question") or "",
                                 "description": (m.get("description") or "")[:4000],
                                 "resolution_source": m.get("resolutionSource") or "",
                                 "end_date": m.get("endDate")}
    with open(REVIEW, "w") as rf:
        for score, a, b, reasons in top:
            rf.write(json.dumps({"a": a, "b": b, "candidate_reasons": reasons,
                                 "score": score, "contract_a": fulltext.get(a, {}),
                                 "contract_b": fulltext.get(b, {})}) + "\n")
    print(f"review batch -> {REVIEW} ({len(top)} pairs w/ full text)")

    print(f"\n===== CANDIDATE PAIRS (structured, price-free) =====")
    print(f"kept: {kept} | cross-event (prize): {cross} | same-event (control): {kept - cross}")
    print("by reason: " + ", ".join(f"{r}={n}" for r, n in sorted(by_reason.items(), key=lambda x: -x[1])))
    print(f"out -> {OUT}\n")
    print("top cross-event candidates:")
    for r in sorted(samples, key=lambda r: r["score"], reverse=True)[:20]:
        print(f"  [{'+'.join(r['candidate_reasons'])}] score={r['score']} "
              f"ents={r['shared_entities']} thr_a={r['thr_a']} thr_b={r['thr_b']}")
        print(f"     A: {r['qa']}")
        print(f"     B: {r['qb']}")
    print("\nNOTE: mechanical candidates only — NOT classified relations, NO prices. "
          "Next: Claude counterexample classifier -> candidate_relations.jsonl")


if __name__ == "__main__":
    main()
