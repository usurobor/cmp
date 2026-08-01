#!/usr/bin/env python3
"""probe_baskets.py — first tradeable-signal probe for Experiment 001.

Goal, not academics: does *exploitable* price incoherence exist in liquid
Polymarket data? The cheapest high-signal test is the mutually-exclusive
basket — a multi-outcome event ("who wins X?") whose YES prices must sum to
~1. Pre-settlement, a basket trading at 1.15 with real depth is a short. This
probe finds those on the corpus we already have. No parquet, no pydantic, no
full pull — stdlib only, runs on the box.

Pipeline (all layers 1-3 collapsed for the ME case):
  1. Load the collected market corpus (streaming, minimal fields).
  2. Group markets by event id  -> related-contract discovery is a mechanical
     join, no semantic classifier needed.
  3. Keep multi-outcome groups, rank by summed volume.
  4. For the top groups, pull each outcome's YES price history from CLOB,
     align on daily buckets, sum across the basket, and flag days where the
     sum deviates from 1 by >= the threshold. That is a candidate historical
     incoherence (NOT proven executable arbitrage — layer 4 is out of scope).

Output: a ranked report to stdout + JSON at $CMP_DATA_DIR/derived/basket_probe.json.

Env:
  CMP_DATA_DIR  corpus root (default /var/lib/cmp/data); globs **/markets_p*.json
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

TOP_GROUPS = int(os.environ.get("PROBE_TOP_GROUPS", "40"))
DEV_THRESHOLD = float(os.environ.get("PROBE_DEV", "0.05"))  # 5 percentage points
MAX_CLOB_CALLS = int(os.environ.get("PROBE_MAX_CLOB", "400"))


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _parse_list(x):
    if isinstance(x, list):
        return x
    if isinstance(x, str):
        try:
            return json.loads(x)
        except json.JSONDecodeError:
            return None
    return None


def _event_key(m: dict):
    """Best-effort event id for grouping. Returns (source, key) or None."""
    ev = m.get("events")
    if isinstance(ev, list) and ev and isinstance(ev[0], dict) and ev[0].get("id"):
        return ev[0]["id"]
    for k in ("eventId", "event_id", "groupSlug", "eventSlug"):
        if m.get(k):
            return m[k]
    return None


def load_groups() -> tuple[dict, dict]:
    pages = sorted(glob.glob(os.path.join(DATA_DIR, "**", "markets_p*.json"), recursive=True))
    print(f"corpus: {len(pages)} pages under {DATA_DIR}")
    if not pages:
        sys.exit(f"no corpus pages found under {DATA_DIR}")

    groups: dict = defaultdict(list)
    total = 0
    sample_keys = None
    grouped = 0
    for p in pages:
        try:
            payload = json.load(open(p))
        except (json.JSONDecodeError, OSError):
            continue
        batch = payload.get("markets") if isinstance(payload, dict) else payload
        for m in batch or []:
            total += 1
            if sample_keys is None:
                sample_keys = sorted(m.keys())
            ek = _event_key(m)
            if ek is None:
                continue
            toks = _parse_list(m.get("clobTokenIds"))
            if not toks:
                continue
            grouped += 1
            groups[ek].append({
                "id": m.get("id"),
                "q": m.get("question") or m.get("groupItemTitle") or "",
                "yes_token": toks[0],
                "outcomes": _parse_list(m.get("outcomes")),
                "vol": _f(m.get("volumeNum") or m.get("volume")),
                "negRisk": m.get("negRisk"),
                "endDate": m.get("endDate"),
            })
    meta = {"total_markets": total, "grouped": grouped,
            "n_events": len(groups), "sample_keys": sample_keys}
    print(f"schema keys on first market:\n  {sample_keys}")
    print(f"markets={total} grouped={grouped} events={len(groups)}")
    return groups, meta


def _get_json(url: str, tries: int = 3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cmp-probe/0.1"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            if i == tries - 1:
                print(f"  ! clob fail {url[:80]}: {e}", file=sys.stderr)
                return None
            time.sleep(2 ** i)
    return None


def yes_history(token: str) -> dict:
    """token -> {day: last_yes_price}. Daily bucket = last print of the UTC day."""
    url = f"{CLOB}/prices-history?market={token}&interval=max&fidelity=1440"
    js = _get_json(url)
    if not js:
        return {}
    hist = js.get("history") if isinstance(js, dict) else js
    daily: dict = {}
    for pt in hist or []:
        t, price = pt.get("t"), pt.get("p")
        if t is None or price is None:
            continue
        day = datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%d")
        daily[day] = float(price)  # later prints overwrite -> last of day
    return daily


def probe_group(members: list) -> dict | None:
    """Fetch each outcome's YES history, align by day, find max basket deviation."""
    series = {}
    for mem in members:
        h = yes_history(mem["yes_token"])
        if h:
            series[mem["id"]] = h
    if len(series) < 2:
        return None
    # days where every outcome has a price
    common = set.intersection(*(set(d) for d in series.values())) if series else set()
    best = None
    for day in common:
        s = sum(series[mid][day] for mid in series)
        dev = abs(s - 1.0)
        if best is None or dev > best["dev"]:
            best = {"day": day, "sum": round(s, 4), "dev": round(dev, 4),
                    "legs": {mid: round(series[mid][day], 4) for mid in series}}
    if best is None:
        return None
    best["n_outcomes"] = len(series)
    best["common_days"] = len(common)
    return best


def main() -> None:
    groups, meta = load_groups()
    multi = [(k, v) for k, v in groups.items() if len(v) >= 2]
    multi.sort(key=lambda kv: sum(m["vol"] for m in kv[1]), reverse=True)
    print(f"\nmulti-outcome events: {len(multi)} (of {len(groups)}). "
          f"probing top {min(TOP_GROUPS, len(multi))} by volume.\n")

    flagged, calls = [], 0
    for ek, members in multi[:TOP_GROUPS]:
        if calls >= MAX_CLOB_CALLS:
            print("hit CLOB call cap, stopping probe loop")
            break
        calls += len(members)
        gv = sum(m["vol"] for m in members)
        res = probe_group(members)
        if not res:
            continue
        row = {"event": ek, "group_volume": round(gv, 2),
               "questions": [m["q"] for m in members][:8], **res}
        hit = res["dev"] >= DEV_THRESHOLD
        mark = "FLAG" if hit else "ok  "
        print(f"[{mark}] vol=${gv:,.0f} n={res['n_outcomes']} "
              f"max_sum={res['sum']} dev={res['dev']} day={res['day']} :: "
              f"{members[0]['q'][:70]}")
        if hit:
            flagged.append(row)

    flagged.sort(key=lambda r: r["dev"], reverse=True)
    os.makedirs(DERIVED, exist_ok=True)
    out = os.path.join(DERIVED, "basket_probe.json")
    json.dump({"meta": meta, "threshold": DEV_THRESHOLD,
               "probed": min(TOP_GROUPS, len(multi)), "flagged": flagged},
              open(out, "w"), indent=2)

    print(f"\n===== SIGNAL =====")
    print(f"multi-outcome events probed: {min(TOP_GROUPS, len(multi))}")
    print(f"flagged (basket dev >= {DEV_THRESHOLD:.0%}): {len(flagged)}")
    for r in flagged[:15]:
        print(f"  dev={r['dev']:.3f} sum={r['sum']} vol=${r['group_volume']:,.0f} "
              f"day={r['day']} :: {r['questions'][0][:60]}")
    print(f"\nreport -> {out}")
    print("NOTE: candidate historical incoherence, NOT proven executable arbitrage.")


if __name__ == "__main__":
    main()
