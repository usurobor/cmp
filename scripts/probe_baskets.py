#!/usr/bin/env python3
"""probe_baskets.py — tradeable-signal probe for Experiment 001 (v3, honest).

Two questions:
  Q1 data adequacy: does CLOB serve usable pre-settlement daily history? Sampled
      across volume+date up front.
  Q2 signal: among liquid multi-outcome events, is there a *buy-the-basket* lock
      -- a day where the mutually-exclusive outcomes' YES prices sum BELOW 1 with
      full coverage. Buy all legs for `sum`, exactly one settles to 1.0, lock
      `1 - sum` (minus fees). That is the clean, directly tradeable direction.
      `sum > 1` is reported too but de-emphasized: for negRisk markets it is
      mostly the structural over-round, not free money.

Two correctness rules that killed v2's inflated numbers:
  - COVERAGE: only compute a basket sum for a day when EVERY outcome of the event
    has a price that day. A partial basket understates the sum -> false buy arb.
  - SUSTAINED: report the daily sum *distribution* (min / median / max / #days),
    not the single most extreme day. A one-day spike in thin early trading is not
    tradeable; a sustained sub-1 basket is.

Corpus: $CMP_DATA_DIR/derived/liquid_volume_ge_10000.jsonl (never the census).
Report: ./exp001_basket_report.json in the workspace (not /var/lib -- perms).
"""

from __future__ import annotations

import glob
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

CLOB = "https://clob.polymarket.com"
DATA_DIR = os.environ.get("CMP_DATA_DIR", "/var/lib/cmp/data")
JSONL = os.path.join(DATA_DIR, "derived", "liquid_volume_ge_10000.jsonl")
REPORT = os.path.join(os.environ.get("GITHUB_WORKSPACE", "."), "exp001_basket_report.json")

TOP_GROUPS = int(os.environ.get("PROBE_TOP_GROUPS", "120"))
BUY_LOCK = float(os.environ.get("PROBE_BUY_LOCK", "0.03"))   # min lock to flag: sum <= 1-0.03
MIN_DAYS = int(os.environ.get("PROBE_MIN_DAYS", "3"))        # sustained: >= this many days
MAX_CLOB_CALLS = int(os.environ.get("PROBE_MAX_CLOB", "600"))
AVAIL_SAMPLE = int(os.environ.get("PROBE_AVAIL", "12"))


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


def _minimal(m: dict):
    toks = _plist(m.get("clobTokenIds"))
    if not toks:
        return None
    return {"id": m.get("id"), "q": m.get("question") or m.get("groupItemTitle") or "",
            "yes_token": toks[0], "vol": _f(m.get("volumeNum") or m.get("volume")),
            "event": _event_key(m), "endDate": (m.get("endDate") or "")[:10],
            "negRisk": m.get("negRisk")}


def load_liquid():
    if not os.path.exists(JSONL):
        sys.exit(f"no liquid corpus at {JSONL} (refusing to scan census)")
    recs, keys = [], None
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


def _get_json(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cmp-probe/0.3"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            if i == tries - 1:
                return None
            time.sleep(2 ** i)
    return None


def daily(token: str) -> dict:
    js = _get_json(f"{CLOB}/prices-history?market={token}&interval=max&fidelity=1440")
    if not js:
        return {}
    h = js.get("history") if isinstance(js, dict) else js
    out = {}
    for pt in h or []:
        t, p = pt.get("t"), pt.get("p")
        if t is not None and p is not None:
            out[datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%d")] = float(p)
    return out


def availability(recs):
    print("\n===== Q1: CLOB HISTORY AVAILABILITY =====")
    by_vol = sorted(recs, key=lambda r: r["vol"], reverse=True)
    dated = sorted((r for r in recs if r["endDate"]), key=lambda r: r["endDate"])
    picks, seen = [], set()
    for r in by_vol[:5] + dated[:4] + dated[-4:]:
        if r["id"] not in seen:
            seen.add(r["id"]); picks.append(r)
    usable = 0
    for r in picks[:AVAIL_SAMPLE]:
        h = daily(r["yes_token"])
        if h:
            d = sorted(h); usable += 1
            print(f"  n={len(h):<4} {d[0]}..{d[-1]}  end={r['endDate']}  :: {r['q'][:45]}")
        else:
            print(f"  n=0   NO HISTORY          end={r['endDate']}  :: {r['q'][:45]}")
    print(f"usable: {usable}/{min(len(picks),AVAIL_SAMPLE)}")
    return usable >= 3


def baskets(recs):
    groups = defaultdict(list)
    for r in recs:
        if r["event"] is not None:
            groups[r["event"]].append(r)
    multi = sorted(((k, v) for k, v in groups.items() if len(v) >= 2),
                   key=lambda kv: sum(m["vol"] for m in kv[1]), reverse=True)
    print(f"\n===== Q2: BUY-THE-BASKET (sum < 1, full coverage) =====")
    print(f"multi-outcome events: {len(multi)}. probing top {min(TOP_GROUPS,len(multi))} by volume.")

    buys, calls, complete = [], 0, 0
    for ek, members in multi[:TOP_GROUPS]:
        if calls >= MAX_CLOB_CALLS:
            print("hit CLOB call cap."); break
        calls += len(members)
        series = {m["id"]: daily(m["yes_token"]) for m in members}
        series = {k: v for k, v in series.items() if v}
        # COVERAGE RULE: need every outcome, else the sum is understated.
        if len(series) < len(members):
            continue
        complete += 1
        common = set.intersection(*(set(v) for v in series.values()))
        if len(common) < MIN_DAYS:
            continue
        sums = sorted((sum(series[i][day] for i in series), day) for day in common)
        vals = [s for s, _ in sums]
        smin, smed, smax = vals[0], statistics.median(vals), vals[-1]
        buy_days = [(s, d) for s, d in sums if s <= 1 - BUY_LOCK]
        gv = sum(m["vol"] for m in members)
        if len(buy_days) >= MIN_DAYS:
            best = buy_days[0]  # deepest lock (lowest sum)
            lock = round(1 - best[0], 4)
            buys.append({"event": ek, "n_outcomes": len(members), "vol": round(gv, 0),
                         "min_sum": round(smin, 4), "median_sum": round(smed, 4),
                         "lock": lock, "buy_days": len(buy_days), "total_days": len(common),
                         "best_day": best[1], "q": members[0]["q"][:70],
                         "negRisk": members[0].get("negRisk")})
            print(f"  [BUY] lock={lock:.1%} min={smin:.3f} med={smed:.3f} "
                  f"buy_days={len(buy_days)}/{len(common)} n={len(members)} "
                  f"vol=${gv:,.0f} :: {members[0]['q'][:55]}")

    buys.sort(key=lambda r: r["lock"] * r["vol"], reverse=True)
    json.dump({"complete_baskets": complete, "buy_candidates": buys},
              open(REPORT, "w"), indent=2)
    print(f"\n===== SIGNAL =====")
    print(f"full-coverage baskets checked: {complete}")
    print(f"buy-the-basket candidates (sum<= {1-BUY_LOCK}, >={MIN_DAYS} days): {len(buys)}")
    for r in buys[:20]:
        print(f"  lock={r['lock']:.1%} med_sum={r['median_sum']} days={r['buy_days']}/{r['total_days']} "
              f"vol=${r['vol']:,.0f} :: {r['q'][:50]}")
    print(f"\nreport -> {REPORT}")
    print("NOTE: candidate historical lock ignoring fees, depth, and fill. NOT proven executable.")


def main():
    recs, keys = load_liquid()
    print(f"liquid markets: {len(recs)} | schema keys: {keys}")
    if not availability(recs):
        print("\nhistory thin -> stop before baskets."); return
    baskets(recs)


if __name__ == "__main__":
    main()
