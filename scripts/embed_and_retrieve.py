#!/usr/bin/env python3
"""embed_and_retrieve.py — Sub 2 embedding-retrieval slice (#3, semantic channel).

QUESTION THIS SLICE TESTS (the pilot left it `underdetermined`):
  Does semantic proximity over contract text surface real logical relations
  between CROSS-EVENT, clean-binary contracts that the *mechanical* channel
  (threshold ladders / neg-risk baskets / shared-entity blocking) could not
  reach?

Design mirrors the pilot's discipline:
  - price-free, outcome-blind, discovery-only;
  - the embedding only RETRIEVES candidates; a downstream blind classifier
    reads full rules and judges — so retrieval needs recall, not precision;
  - retained candidates are deliberately MECHANICALLY INVISIBLE (not same event,
    not same ladder template, not >=2 shared entities) so a positive here is
    something mechanical retrieval structurally missed;
  - known mechanical positives are INJECTED as a recall-calibration stratum.

MEMORY-BOUNDED for the 2 GB / 1 vCPU box (box installed MemoryMax=1400M):
  - clean-binary filter streams the corpus (O(1));
  - encode streams texts in batches to an on-disk float32 memmap (never holds
    all texts or all vectors + model at once);
  - retrieval loads the index once (~N*384*4 bytes) and chunks the query side.

Stages (idempotent where expensive):
  1  clean-binary filter        -> $DATA/derived/embeddings/cb.jsonl   (+ signals)
  2  encode (skip if present)   -> $DATA/derived/embeddings/cb.f32, ids.npy
  3  semantic kNN retrieval     -> $DATA/derived/embeddings/semantic_pairs.jsonl
  4  blinded stratified batch   -> ./derived/classification_input.jsonl
                                   ./derived/evaluation_manifest.jsonl  (committed)

Corpus: $CMP_DATA_DIR/derived/liquid_volume_ge_10000.jsonl
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

DATA_DIR = os.environ.get("CMP_DATA_DIR", "/var/lib/cmp/data")
JSONL = os.path.join(DATA_DIR, "derived", "liquid_volume_ge_10000.jsonl")
EMB_DIR = os.path.join(DATA_DIR, "derived", "embeddings")
CB = os.path.join(EMB_DIR, "cb.jsonl")
F32 = os.path.join(EMB_DIR, "cb.f32")
IDS = os.path.join(EMB_DIR, "ids.npy")
PAIRS = os.path.join(EMB_DIR, "semantic_pairs.jsonl")

OUT_DIR = os.path.join(os.environ.get("GITHUB_WORKSPACE", "."), "derived")
INPUT = os.path.join(OUT_DIR, "classification_input.jsonl")
MANIFEST = os.path.join(OUT_DIR, "evaluation_manifest.jsonl")

MODEL_NAME = os.environ.get("EMB_MODEL", "BAAI/bge-small-en-v1.5")
DIM = 384
SEED = int(os.environ.get("BATCH_SEED", "20260801"))
BATCH = int(os.environ.get("ENC_BATCH", "256"))
QCHUNK = int(os.environ.get("QUERY_CHUNK", "256"))
K = int(os.environ.get("KNN", "10"))
SIM_FLOOR = float(os.environ.get("SIM_FLOOR", "0.55"))
KEEP_CAP = int(os.environ.get("KEEP_CAP", "8000"))
N_SEMANTIC = int(os.environ.get("N_SEMANTIC", "150"))   # blind pairs to judge
N_INJECT = int(os.environ.get("N_INJECT", "15"))        # recall-calibration positives
MAX_MARKETS = int(os.environ.get("MAX_MARKETS", "0"))   # 0 = all clean-binary

NUM_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?")
UP = re.compile(r"\b(above|over|greater|exceed|at least|>=|≥|more than|reach)\b", re.I)
ENT_RE = re.compile(r"\b([A-Z][a-zA-Z0-9.&'-]+(?:\s+[A-Z][a-zA-Z0-9.&'-]+){0,3})\b")
STOP_ENT = {"will", "the", "yes", "no", "us", "u.s", "u.s.", "usa"} | {str(y) for y in range(2018, 2031)}
# A market carrying any of these in its rules is NOT clean binary (void/50-50/
# tie/refund => binary_with_void_or_refund or multi_outcome). Conservative on
# purpose: over-dropping shrinks the pool but keeps the {0,1} settlement domain
# where the 2x2 classifier is valid.
VOID_RE = re.compile(
    r"50\s*[-/]\s*50|50\s*percent|resolve[^.]{0,25}\b50\b|\btie\b|\bvoid\b|"
    r"cancel|postpone|refund|invalid|abandoned|no\s+contest|walk\s*over|"
    r"\bdraw\b|split\s+evenly|\bn/?a\b",
    re.I,
)


def digest(*p) -> str:
    return hashlib.sha256("".join(str(x or "") for x in p).encode()).hexdigest()[:16]


def norm_no_num(q: str) -> str:
    return NUM_RE.sub("#", (q or "").lower()).strip()


def ents(text: str) -> list:
    out = []
    seen = set()
    for e in ENT_RE.findall(text or ""):
        low = e.lower()
        if low not in STOP_ENT and len(low) >= 3 and low not in seen:
            seen.add(low)
            out.append(low)
        if len(out) >= 6:
            break
    return out


def as_tokens(clob) -> int:
    if clob is None:
        return 0
    if isinstance(clob, str):
        try:
            clob = json.loads(clob)
        except json.JSONDecodeError:
            return 0
    return len(clob) if isinstance(clob, list) else 0


# ---------------------------------------------------------------- stage 1
def stage1_clean_binary() -> int:
    os.makedirs(EMB_DIR, exist_ok=True)
    n_in = n_bin = n_clean = 0
    t0 = time.time()
    with open(JSONL) as fh, open(CB, "w") as out:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_in += 1
            if as_tokens(m.get("clobTokenIds")) != 2:
                continue
            n_bin += 1
            desc = m.get("description") or ""
            q = m.get("question") or ""
            if VOID_RE.search(desc) or VOID_RE.search(q):
                continue          # not clean binary
            n_clean += 1
            src = (m.get("resolutionSource") or "").strip().lower()
            end = (m.get("endDate") or "")[:10]
            ev = m.get("events")
            eid = ev[0]["id"] if isinstance(ev, list) and ev and isinstance(ev[0], dict) else m.get("eventId")
            nums = [x for x in NUM_RE.findall(q)]
            ladder = digest(norm_no_num(q), src, end) if (nums and UP.search(q)) else ""
            # retrieval text: question + condition head (before boilerplate), truncated
            text = (q + " || " + desc[:400]).strip()[:1800]
            out.write(json.dumps({
                "id": m.get("id"), "text": text,
                "event": str(eid) if eid else "",
                "ladder": ladder,
                "negrisk": bool(m.get("negRisk")),
                "ents": ents(q),
            }) + "\n")
            if MAX_MARKETS and n_clean >= MAX_MARKETS:
                break
    print(f"[1] corpus={n_in} binary={n_bin} clean_binary={n_clean} "
          f"({100*n_clean/max(n_in,1):.1f}% of corpus)  {time.time()-t0:.0f}s")
    return n_clean


def cb_count() -> int:
    n = 0
    with open(CB) as fh:
        for _ in fh:
            n += 1
    return n


# ---------------------------------------------------------------- stage 2
def stage2_encode(n: int) -> None:
    if os.path.exists(F32) and os.path.exists(IDS):
        existing = np.load(IDS, allow_pickle=True)
        if len(existing) == n:
            print(f"[2] encode SKIP — {n} vectors already present at {F32}")
            return
        print(f"[2] stale index ({len(existing)} != {n}); re-encoding")

    from fastembed import TextEmbedding
    print(f"[2] loading model {MODEL_NAME} ...")
    t0 = time.time()
    model = TextEmbedding(MODEL_NAME)
    probe = list(model.embed(["self-test"]))
    assert probe and probe[0].shape[0] == DIM, f"unexpected dim {probe[0].shape}"
    print(f"[2] model ready ({time.time()-t0:.0f}s), dim={DIM}, encoding {n} texts")

    mm = np.memmap(F32, dtype="float32", mode="w+", shape=(n, DIM))
    ids = []
    buf = []
    row = 0
    t0 = time.time()

    def flush(texts):
        nonlocal row
        if not texts:
            return
        embs = np.asarray(list(model.embed(texts)), dtype="float32")
        embs /= (np.linalg.norm(embs, axis=1, keepdims=True) + 1e-9)
        mm[row:row + len(embs)] = embs
        row += len(embs)

    with open(CB) as fh:
        for line in fh:
            rec = json.loads(line)
            ids.append(rec["id"])
            buf.append(rec["text"] or rec["id"])
            if len(buf) >= BATCH:
                flush(buf)
                buf = []
                if row % (BATCH * 40) == 0:
                    rate = row / max(time.time() - t0, 1e-6)
                    eta = (n - row) / max(rate, 1e-6)
                    print(f"[2]   {row}/{n}  {rate:.0f}/s  eta {eta/60:.0f}m", flush=True)
        flush(buf)
    mm.flush()
    del mm
    np.save(IDS, np.array(ids, dtype=object))
    print(f"[2] encoded {row} vectors in {(time.time()-t0)/60:.1f}m -> {F32}")


# ---------------------------------------------------------------- stage 3
def stage3_retrieve(n: int):
    ids = list(np.load(IDS, allow_pickle=True))
    idx = np.array(np.memmap(F32, dtype="float32", mode="r", shape=(n, DIM)))  # ~n*1.5KB
    # aligned signal arrays (same order as cb.jsonl == encode order)
    events, ladders, negr, entl = [], [], [], []
    with open(CB) as fh:
        for line in fh:
            r = json.loads(line)
            events.append(r["event"]); ladders.append(r["ladder"])
            negr.append(r["negrisk"]); entl.append(frozenset(r["ents"]))
    assert len(events) == n == len(ids), "alignment broken"

    def mechanically_visible(i, j) -> bool:
        if events[i] and events[i] == events[j]:
            return True
        if ladders[i] and ladders[i] == ladders[j]:
            return True
        if negr[i] and negr[j] and events[i] and events[i] == events[j]:
            return True
        if len(entl[i] & entl[j]) >= 2:
            return True
        return False

    kept = {}   # (i,j) -> sim   ; semantic-only cross-event pairs
    t0 = time.time()
    for s in range(0, n, QCHUNK):
        q = idx[s:s + QCHUNK]                    # (c, DIM)
        sims = q @ idx.T                         # (c, n)
        for r in range(q.shape[0]):
            i = s + r
            row = sims[r]
            row[i] = -2.0                        # exclude self
            top = np.argpartition(row, -K)[-K:]
            for j in top:
                j = int(j)
                sv = float(row[j])
                if sv < SIM_FLOOR:
                    continue
                a, b = (i, j) if i < j else (j, i)
                if a == b:
                    continue
                if mechanically_visible(a, b):
                    continue
                prev = kept.get((a, b))
                if prev is None or sv > prev:
                    kept[(a, b)] = sv
        if s % (QCHUNK * 20) == 0:
            print(f"[3]   {s}/{n} queried, kept={len(kept)}  {time.time()-t0:.0f}s", flush=True)

    pairs = sorted(kept.items(), key=lambda kv: kv[1], reverse=True)[:KEEP_CAP]
    with open(PAIRS, "w") as out:
        for (a, b), sv in pairs:
            out.write(json.dumps({"a_id": ids[a], "b_id": ids[b], "sim": round(sv, 4),
                                  "a_row": a, "b_row": b}) + "\n")
    if pairs:
        sv = [p[1] for p in pairs]
        print(f"[3] semantic-only cross-event pairs kept={len(pairs)}  "
              f"sim[min/med/max]={min(sv):.3f}/{sv[len(sv)//2]:.3f}/{max(sv):.3f}  "
              f"{(time.time()-t0)/60:.1f}m")
    else:
        print("[3] NO pairs kept — check SIM_FLOOR / filters")
    return ids, events, ladders, negr


# ---------------------------------------------------------------- stage 4
def stage4_batch(n: int, ids, events, ladders):
    rng = random.Random(SEED)

    # semantic strata: sample across similarity bands (blind to relation)
    sem = [json.loads(l) for l in open(PAIRS)] if os.path.exists(PAIRS) else []
    chosen = []   # (a_id, b_id, stratum, expected, channel)
    if sem:
        hi = sem[: len(sem) // 3]
        mid = sem[len(sem) // 3: 2 * len(sem) // 3]
        lo = sem[2 * len(sem) // 3:]
        per = max(N_SEMANTIC // 3, 1)
        for band, label in ((hi, "S_high"), (mid, "S_mid"), (lo, "S_low")):
            rng.shuffle(band)
            for p in band[:per]:
                chosen.append((p["a_id"], p["b_id"], label, "unknown",
                               ["semantic_knn", "cross_event", "mechanically_invisible",
                                f"sim={p['sim']}"]))

    # injected recall-calibration positives from clean-binary threshold ladders
    lad = defaultdict(list)
    for i in range(n):
        if ladders[i]:
            lad[ladders[i]].append(i)
    posrows = []
    for key, members in lad.items():
        if len(members) >= 2:
            posrows.append(members)
    rng.shuffle(posrows)
    inj = 0
    for members in posrows:
        if inj >= N_INJECT:
            break
        members = members[:2]
        a, b = ids[members[0]], ids[members[1]]
        chosen.append((a, b, "INJ_pos", "a_implies_b_or_ladder",
                       ["mechanical_positive_injected", "threshold_ladder"]))
        inj += 1

    # pass 2: full text ONLY for chosen ids (bounded re-read)
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
                                if re.search(r"void|cancel|n/?a|refund|invalid|50", s, re.I))[:600]
                full[mid] = {"question": m.get("question") or "", "full_rules": desc[:6000],
                             "resolution_source": m.get("resolutionSource") or "",
                             "end_date": m.get("endDate"), "void_conditions": void or None,
                             "digest": digest(m.get("question"), desc, m.get("resolutionSource"), m.get("endDate"))}
                if len(full) == len(want):
                    break

    observed = datetime.now(timezone.utc).isoformat()
    os.makedirs(OUT_DIR, exist_ok=True)
    written = 0
    with open(INPUT, "w") as fin, open(MANIFEST, "w") as fman:
        for a, b, stratum, expected, channel in chosen:
            fa, fb = full.get(a), full.get(b)
            if not fa or not fb:
                continue
            fin.write(json.dumps({
                "a_id": a, "b_id": b,
                "contract_a": {k: fa.get(k) for k in ("question", "full_rules", "resolution_source", "end_date", "void_conditions")},
                "contract_b": {k: fb.get(k) for k in ("question", "full_rules", "resolution_source", "end_date", "void_conditions")},
                "a_version_digest": fa.get("digest"), "b_version_digest": fb.get("digest"),
                "text_observed_at": observed, "version_basis": "final_api_snapshot",
                "historical_contract_version": "unavailable", "historical_discoverability": "not_established"}) + "\n")
            fman.write(json.dumps({"a_id": a, "b_id": b, "stratum": stratum,
                                   "expected_relation": expected, "retrieval_channel": channel}) + "\n")
            written += 1

    from collections import Counter
    dist = Counter(c[2] for c in chosen)
    print(f"[4] batch written={written}  strata={dict(dist)}")
    print(f"[4] input -> {INPUT}\n[4] manifest -> {MANIFEST}")


def main() -> None:
    if not os.path.exists(JSONL):
        raise SystemExit(f"no liquid corpus at {JSONL}")
    if os.environ.get("SKIP_FILTER") == "1" and os.path.exists(CB):
        n = cb_count()
        print(f"[1] SKIP filter — reuse {n} clean-binary from {CB}")
    else:
        n = stage1_clean_binary()
    if n < 2:
        raise SystemExit("too few clean-binary markets")
    stage2_encode(n)
    ids, events, ladders, negr = stage3_retrieve(n)
    stage4_batch(n, ids, events, ladders)
    print("DONE")


if __name__ == "__main__":
    main()
