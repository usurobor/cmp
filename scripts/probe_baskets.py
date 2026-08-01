#!/usr/bin/env python3
"""probe_baskets.py — tradeable-signal probe for Experiment 001 (lean v2).

Two questions, answered in order:

  Q1 (data adequacy): does CLOB actually serve usable pre-settlement price
      history for these settled markets? Sampled up front across the volume
      range AND the date range (old vs recent), reported before anything else.
      A basket check is impossible without history — so this gates the rest.

  Q2 (signal): among liquid multi-outcome events, do the outcome YES prices sum
      far enough from 1.0, pre-settlement, to be a candidate mispricing? Group
      by event (mechanical join, no classifier), sum each basket's daily YES
      histories, flag deviation >= threshold. Candidate historical incoherence
      only — NOT proven executable arbitrage (layer 4, out of scope).

Corpus source (in priority order; the huge unfiltered census is never scanned):
  1. $CMP_DATA_DIR/derived/liquid_volume_ge_10000.jsonl  (one market per line)
  2. $CMP_DATA_DIR/raw/polymarket-vol*/markets_p*.json    (a filtered pull)

Env: CMP_DATA_DIR (default /var/lib/cmp/data)
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

CLOB = "https://clob.polymarket.com"
DATA_DIR = os.environ.get("CMP_DATA_DIR", "/var/lib/cmp/data")
DERIVED = os.path.join(DATA_DIR, "derived")
JSONL = os.path.join(DERIVED, "liquid_volume_ge_10000.jsonl")

TOP_GROUPS = int(os.environ.get("PROBE_TOP_GROUPS", "40"))
DEV_THRESHOLD = float(os.environ.get("PROBE_DEV", "0.05"))
MAX_CLOB_CALLS = int(os.environ.get("PROBE_MAX_CLOB", "250"))
AVAIL_SAMPLE = int(os.environ.get("PROBE_AVAIL", "18"))


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _plist(x):
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            return json.loads(x)
        except json.JSONDecodeError:
            return None
    return None


def _event_key(m: dict):
    ev = m.get("events")
    if isinstance(ev, list) and ev and isinstance(ev[0], dict) and ev[0].get("id"):
        return ev[0]["id"]
    for k in ("eventId", "event_id", "groupSlug", "eventSlug"):
        if m.get(k):
            return m[k]
    return None


def _minimal(m: dict) -> dict | None:
    toks = _plist(m.get("clobTokenIds"))
    if not toks:
        return None
    return {
        "id": m.get("id"),
        "q": m.get("question") or m.get("groupItemTitle") or "",
        "yes_token": toks[0],
        "vol": _f(m.get("volumeNum") or m.get("volume")),
        "event": _event_key(m),
        "endDate": (m.get("endDate") or "")[:10],
        "negRisk": m.get("negRisk"),
    }


def load_liquid() -> tuple[list[dict], list | None]:
    """Return (minimal market records, sample_keys). Never scans the census."""
    recs, keys = [], None
    if os.path.exists(JSONL):
        print(f"corpus: {JSONL}")
        with open(JSONL) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if keys is None:
                    keys = sorted(m.keys())
                r = _minimal(m)
                if r:
                    recs.append(r)
        return recs, keys

    pages = sorted(glob.glob(os.path.join(DATA_DIR, "raw", "polymarket-vol*",
                                          "markets_p*.json")))
    if not pages:
        sys.exit(f"no liquid corpus found: neither {JSONL} nor raw/polymarket-vol*/ "
                 f"exists under {DATA_DIR}. (Refusing to scan the full census.)")
    print(f"corpus: {len(pages)} filtered pages under {DATA_DIR}/raw/polymarket-vol*")
    for p in pages:
        try:
            payload = json.load(open(p))
        except (json.JSONDecodeError, OSError):
            continue
        for m in (payload.get("markets") if isinstance(payload, dict) else payload) or []:
            if keys is None:
                keys = sorted(m.keys())
            r = _minimal(m)
            if r:
                recs.append(r)
    return recs, keys


def _get_json(url: str, tries: int = 3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cmp-probe/0.2"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            if i == tries - 1:
                return None
            time.sleep(2 ** i)
    return None


def history(token: str) -> list:
    js = _get_json(f"{CLOB}/prices-history?market={token}&interval=max&fidelity=1440")
    if not js:
        return []
    h = js.get("history") if isinstance(js, dict) else js
    return h or []


def _daily(hist: list) -> dict:
    out = {}
    for pt in hist:
        t, p = pt.get("t"), pt.get("p")
        if t is None or p is None:
            continue
        out[datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%d")] = float(p)
    return out


def availability(recs: list[dict]) -> bool:
    """Sample across volume AND date; report CLOB coverage. Returns True if usable."""
    print("\n===== Q1: CLOB PRICE-HISTORY AVAILABILITY =====")
    by_vol = sorted(recs, key=lambda r: r["vol"], reverse=True)
    dated = [r for r in recs if r["endDate"]]
    by_date = sorted(dated, key=lambda r: r["endDate"])
    picks, seen = [], set()
    # top volume, oldest, newest, and a volume-spread middle
    cand = by_vol[:6] + by_date[:4] + by_date[-4:] + by_vol[len(by_vol)//2:len(by_vol)//2+4]
    for r in cand:
        if r["id"] not in seen:
            seen.add(r["id"]); picks.append(r)
        if len(picks) >= AVAIL_SAMPLE:
            break
    usable = 0
    for r in picks:
        h = _daily(history(r["yes_token"]))
        if h:
            days = sorted(h)
            span = f"{days[0]}..{days[-1]}"
            n = len(h)
            if n >= 5:
                usable += 1
            print(f"  n={n:<5} span={span}  end={r['endDate']}  vol=${r['vol']:,.0f}  "
                  f":: {r['q'][:50]}")
        else:
            print(f"  n=0     NO HISTORY            end={r['endDate']}  vol=${r['vol']:,.0f}  "
                  f":: {r['q'][:50]}")
    print(f"\nusable (>=5 daily points): {usable}/{len(picks)} sampled")
    verdict = usable >= max(3, len(picks) // 3)
    print("VERDICT: " + ("history is available — basket check is viable"
                         if verdict else
                         "history is thin/absent — basket check NOT viable from CLOB"))
    return verdict


def baskets(recs: list[dict]) -> None:
    groups = defaultdict(list)
    for r in recs:
        if r["event"] is not None:
            groups[r["event"]].append(r)
    multi = [(k, v) for k, v in groups.items() if len(v) >= 2]
    multi.sort(key=lambda kv: sum(m["vol"] for m in kv[1]), reverse=True)
    print(f"\n===== Q2: BASKET SUMS =====")
    print(f"events with >=2 outcomes: {len(multi)} (of {len(groups)}). "
          f"probing top {min(TOP_GROUPS, len(multi))} by volume.")
    if not multi:
        print("no multi-outcome groups — event grouping field may be missing; "
              "see schema keys above.")
        return

    flagged, calls = [], 0
    for ek, members in multi[:TOP_GROUPS]:
        if calls >= MAX_CLOB_CALLS:
            print("hit CLOB call cap, stopping.")
            break
        calls += len(members)
        series = {}
        for mem in members:
            d = _daily(history(mem["yes_token"]))
            if d:
                series[mem["id"]] = d
        if len(series) < 2:
            continue
        common = set.intersection(*(set(d) for d in series.values()))
        if not common:
            continue
        best = max(((day, sum(series[i][day] for i in series)) for day in common),
                   key=lambda ds: abs(ds[1] - 1.0))
        dev = abs(best[1] - 1.0)
        gv = sum(m["vol"] for m in members)
        mark = "FLAG" if dev >= DEV_THRESHOLD else "ok  "
        print(f"[{mark}] dev={dev:.3f} sum={best[1]:.3f} n={len(series)} "
              f"vol=${gv:,.0f} day={best[0]} :: {members[0]['q'][:60]}")
        if dev >= DEV_THRESHOLD:
            flagged.append({"event": ek, "dev": round(dev, 4), "sum": round(best[1], 4),
                            "day": best[0], "n": len(series), "vol": round(gv, 2),
                            "questions": [m["q"] for m in members][:8]})

    flagged.sort(key=lambda r: r["dev"], reverse=True)
    os.makedirs(DERIVED, exist_ok=True)
    json.dump(flagged, open(os.path.join(DERIVED, "basket_probe.json"), "w"), indent=2)
    print(f"\n===== SIGNAL =====")
    print(f"flagged baskets (dev >= {DEV_THRESHOLD:.0%}): {len(flagged)}")
    for r in flagged[:15]:
        print(f"  dev={r['dev']:.3f} sum={r['sum']} vol=${r['vol']:,.0f} "
              f"day={r['day']} :: {r['questions'][0][:55]}")
    print("NOTE: candidate historical incoherence, NOT executable arbitrage.")


def main() -> None:
    recs, keys = load_liquid()
    print(f"liquid markets loaded: {len(recs)}")
    print(f"schema keys: {keys}")
    if not recs:
        sys.exit("no usable market records")
    ok = availability(recs)
    if not ok:
        print("\nStopping before baskets: without price history the sum check is "
              "meaningless. Next: target recently-settled events or another source.")
        return
    baskets(recs)


if __name__ == "__main__":
    main()
