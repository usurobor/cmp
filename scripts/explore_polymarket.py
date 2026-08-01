#!/usr/bin/env python3
"""explore_polymarket.py — EXPLORATORY, quick-and-dirty Polymarket collector.

Experiment 001, Sub 1 (#2), manual exploration stage. This is NOT the hardened
collector the #2 contract specifies (no typer CLI, no pydantic schema, no tests,
no content-addressed store). Its only job is to *get the data* so we can eyeball
what Polymarket actually returns. Expect this file to be thrown away / rewritten
once we know the real field shapes.

Design choices for "run anywhere with zero setup":
  - stdlib only (urllib) — no pip install needed.
  - dumps each API page to disk verbatim (bytes as returned) under data/raw/,
    so raw snapshots are immutable and we never have to re-pull to reproduce.
  - writes a small tracked manifest (counts, endpoints, per-file sha256, rough
    time range, coverage) OUTSIDE data/ so it can be committed.

Run:
    python3 scripts/explore_polymarket.py                 # collect closed markets
    python3 scripts/explore_polymarket.py --max-pages 4   # quick smoke test
    python3 scripts/explore_polymarket.py --price-samples 25

NOTE: requires outbound network to *.polymarket.com. In the Claude Code web
sandbox this is blocked by the egress policy (only package registries + GitHub
are reachable), so run it somewhere with open egress (e.g. your laptop).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

RAW_DIR = Path("data/raw/polymarket")
PRICES_DIR = RAW_DIR / "prices"
MANIFEST_PATH = Path("docs/experiment-001/manifest.json")  # tracked (outside data/)


def _get(url: str, tries: int = 4, timeout: int = 40) -> bytes:
    """GET with naive exponential backoff. Returns raw response bytes."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cmp-exp001/0.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError) as e:  # noqa: PERF203
            last = e
            wait = 2 ** i
            print(f"  ! {url} failed ({e}); retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise SystemExit(f"giving up on {url}: {last}")


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def collect_markets(limit: int, max_pages: int | None) -> list[dict]:
    """Page through closed markets, dumping each page verbatim. Returns parsed markets."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    snapshots: list[dict] = []
    all_markets: list[dict] = []
    offset = 0
    page = 0
    while True:
        if max_pages is not None and page >= max_pages:
            print(f"reached --max-pages={max_pages}, stopping")
            break
        url = (
            f"{GAMMA}/markets?closed=true&limit={limit}&offset={offset}"
            f"&order=id&ascending=true"
        )
        print(f"page {page} offset={offset} -> {url}")
        body = _get(url)
        out = RAW_DIR / f"markets_off{offset:07d}.json"
        out.write_bytes(body)  # immutable raw snapshot, exactly as returned
        snapshots.append({"file": str(out), "sha256": _sha256(body), "url": url})

        try:
            batch = json.loads(body)
        except json.JSONDecodeError as e:
            raise SystemExit(f"non-JSON response at offset {offset}: {e}")
        if isinstance(batch, dict):  # some endpoints wrap in {"data": [...]}
            batch = batch.get("data") or batch.get("markets") or []
        if not batch:
            print("empty page, reached end of closed markets")
            break
        all_markets.extend(batch)
        offset += limit
        page += 1

    _write_manifest(snapshots, all_markets, limit)
    return all_markets


def sample_price_history(markets: list[dict], n: int) -> None:
    """Grab price history for the first n markets that expose CLOB token ids."""
    if n <= 0:
        return
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    got = 0
    for m in markets:
        if got >= n:
            break
        raw_tokens = m.get("clobTokenIds")
        if not raw_tokens:
            continue
        try:
            token_ids = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
        except json.JSONDecodeError:
            continue
        if not token_ids:
            continue
        token = token_ids[0]
        url = f"{CLOB}/prices-history?market={token}&interval=max&fidelity=60"
        try:
            body = _get(url, tries=2)
        except SystemExit:
            continue
        mid = m.get("id", "unknown")
        (PRICES_DIR / f"{mid}_{token}.json").write_bytes(body)
        got += 1
        print(f"  price history {got}/{n}: market {mid}")
    print(f"sampled price history for {got} markets")


def _rough_time_range(markets: list[dict]) -> dict:
    """Best-effort min/max endDate; tolerant of missing/odd fields."""
    ends = []
    for m in markets:
        v = m.get("endDate") or m.get("end_date")
        if isinstance(v, str) and v:
            ends.append(v)
    ends.sort()
    return {"earliest_endDate": ends[0] if ends else None,
            "latest_endDate": ends[-1] if ends else None}


def _write_manifest(snapshots: list[dict], markets: list[dict], limit: int) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "experiment": "001",
        "stage": "exploratory-collection (Sub 1 / #2) — NOT hardened",
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_endpoints": [f"{GAMMA}/markets?closed=true", f"{CLOB}/prices-history"],
        "page_limit": limit,
        "total_markets": len(markets),
        "time_range": _rough_time_range(markets),
        "snapshots": snapshots,
        "coverage_note": (
            "Exploratory pull of closed Polymarket markets via the public Gamma "
            "API. Coverage is whatever the API paged out in this run; verify the "
            "count against the ≥5,000 / full-coverage requirement in #2 before "
            "treating this as the corpus."
        ),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"wrote manifest -> {MANIFEST_PATH}")


def _peek(markets: list[dict]) -> None:
    """Print what's actually there so a human can eyeball the shape."""
    print("\n===== WHAT'S THERE =====")
    print(f"total closed markets collected: {len(markets)}")
    if not markets:
        return
    first = markets[0]
    print(f"\nfield keys on first market ({len(first)} fields):")
    for k in sorted(first):
        val = first[k]
        preview = str(val)
        if len(preview) > 80:
            preview = preview[:77] + "..."
        print(f"  {k}: {preview}")
    with_prices = sum(1 for m in markets if m.get("clobTokenIds"))
    with_rules = sum(1 for m in markets if m.get("description") or m.get("resolutionSource"))
    print(f"\nmarkets exposing clobTokenIds (price-history capable): {with_prices}")
    print(f"markets with description/resolutionSource text: {with_rules}")
    print(f"rough endDate range: {_rough_time_range(markets)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Exploratory Polymarket closed-market collector.")
    ap.add_argument("--limit", type=int, default=500, help="page size (Gamma max ~500)")
    ap.add_argument("--max-pages", type=int, default=None, help="cap pages (smoke test)")
    ap.add_argument("--price-samples", type=int, default=10,
                    help="how many markets to pull price history for (0 to skip)")
    args = ap.parse_args()

    markets = collect_markets(limit=args.limit, max_pages=args.max_pages)
    sample_price_history(markets, args.price_samples)
    _peek(markets)
    print("\ndone. raw snapshots under data/raw/polymarket/ (gitignored),"
          f" manifest at {MANIFEST_PATH} (tracked).")


if __name__ == "__main__":
    main()
