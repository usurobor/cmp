#!/usr/bin/env python3
"""explore_polymarket.py — EXPLORATORY, quick-and-dirty Polymarket collector.

Experiment 001, Sub 1 (#2), manual exploration stage. This is NOT the hardened
collector the #2 contract specifies (no typer CLI, no pydantic schema, no tests,
no content-addressed store). Its only job is to *get the data* so we can eyeball
what Polymarket actually returns.

Design choices for "run anywhere with zero setup":
  - stdlib only (urllib) — no pip install needed.
  - dumps each API page to disk verbatim (bytes as returned) under data/raw/,
    so raw snapshots are immutable and we never have to re-pull to reproduce.
  - writes a small tracked manifest (counts, endpoints, per-file sha256, rough
    time range, coverage) OUTSIDE data/ so it can be committed.

PAGINATION (why keyset, not offset)
    The Gamma /markets endpoint hard-caps offset at ~2000:
        HTTP 422 {"error":"offset too large, use /markets/keyset for deeper
                  pagination"}
    so offset paging can NEVER reach the full closed-market corpus. This
    collector uses /markets/keyset with the `after_cursor` parameter, which
    pages the whole corpus with strictly-ascending ids. Two traps this code
    guards against, both observed live against the real API:
      1. `limit` is silently capped at 100 per page. Advancing a cursor/offset
         by the *requested* limit rather than the *returned* count skips
         records (the previous version of this script advanced by 500 and so
         collected ~20% of the corpus while reporting a complete pull).
      2. An unrecognized cursor parameter name is *ignored*, not rejected —
         the API cheerfully returns page 0 again. A naive loop spins forever
         on page 0. We detect a non-advancing cursor and stop.

ERROR HANDLING
    - Retries only what is actually transient (429/5xx/network/timeout), with
      exponential backoff + jitter, honouring Retry-After.
    - Fails fast on non-retryable 4xx (a validation error will never succeed
      on retry) and surfaces the response body.
    - The manifest is ALWAYS written — on success, on error, and on Ctrl-C —
      and records `complete: true|false` plus `stopped_reason`. A partial pull
      is a legitimate artifact; a *silently* partial one is not.

Run:
    python3 scripts/explore_polymarket.py                 # full closed-market pull
    python3 scripts/explore_polymarket.py --max-pages 4   # quick smoke test
    python3 scripts/explore_polymarket.py --price-samples 25

NOTE: requires outbound network to *.polymarket.com.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# The corpus is the durable artifact and must not live inside the CI
# workspace: actions/checkout wipes gitignored paths, and re-registering
# the runner deletes _work entirely. CMP_DATA_DIR points at storage owned
# by the box (/var/lib/cmp/data in production) so the GitHub Actions
# wrapper -- a throwaway egress workaround, not part of the experiment --
# can be deleted without taking the corpus with it. The relative default
# keeps local runs and tests working unchanged.
DATA_DIR = Path(os.environ.get("CMP_DATA_DIR") or "data")
RAW_DIR = DATA_DIR / "raw" / "polymarket"
PRICES_DIR = RAW_DIR / "prices"
CHECKPOINT_PATH = RAW_DIR / "_checkpoint.json"
MANIFEST_PATH = Path("docs/experiment-001/manifest.json")  # tracked (outside data/)

USER_AGENT = "cmp-exp001/0.1"
PAGE_CAP = 100  # server-side ceiling on `limit`, regardless of what we ask for
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """A request failed permanently (non-retryable, or retries exhausted)."""


def _get(url: str, tries: int = 5, timeout: int = 40) -> bytes:
    """GET with backoff. Retries transient failures only; raises FetchError otherwise."""
    last = ""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode("utf-8", "replace").strip()
            if e.code not in RETRYABLE_STATUS:
                # 4xx validation errors never succeed on retry — fail immediately
                # with the server's own explanation rather than burning backoff.
                raise FetchError(f"HTTP {e.code} (non-retryable) for {url}: {body}") from e
            last = f"HTTP {e.code}: {body}"
            retry_after = e.headers.get("Retry-After") if e.headers else None
            wait = float(retry_after) if (retry_after or "").isdigit() else None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = repr(e)
            wait = None

        if attempt == tries - 1:
            break
        if wait is None:
            wait = min(2**attempt, 30) + random.uniform(0, 0.5)
        print(f"  ! {last} — retry {attempt + 1}/{tries - 1} in {wait:.1f}s", file=sys.stderr)
        time.sleep(wait)

    raise FetchError(f"giving up on {url} after {tries} attempts: {last}")


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _keyset_url(limit: int, cursor: str | None) -> str:
    params = {
        "closed": "true",
        "limit": str(limit),
        "order": "id",
        "ascending": "true",
    }
    if cursor:
        params["after_cursor"] = cursor
    return f"{GAMMA}/markets/keyset?{urllib.parse.urlencode(params)}"


def _page_path(page: int) -> Path:
    return RAW_DIR / f"markets_p{page:05d}.json"


class MarketStats:
    """Streaming aggregate over the corpus.

    The raw pages are already durably on disk before this ever sees a record,
    so retaining the parsed dicts only to compute a handful of summary values
    is pure duplication — and it grows without bound, which is what OOM-killed
    the full run. Everything the manifest, the peek, and the price sampler
    actually need is a reduction: counts, a running min/max, the first record,
    and the first few price-capable candidates. All O(1) in corpus size.
    """

    __slots__ = ("count", "with_prices", "with_rules", "earliest", "latest",
                 "first", "price_candidates", "_want")

    # Keep a cushion beyond --price-samples: sample_price_history skips
    # candidates whose history fetch fails, and the old code could walk the
    # entire corpus looking for replacements.
    CANDIDATE_CUSHION = 20

    def __init__(self, price_samples: int = 0) -> None:
        self.count = 0
        self.with_prices = 0
        self.with_rules = 0
        self.earliest: str | None = None
        self.latest: str | None = None
        self.first: dict | None = None
        self.price_candidates: list[dict] = []
        self._want = max(price_samples, 0) + (self.CANDIDATE_CUSHION
                                              if price_samples > 0 else 0)

    def add(self, m: dict) -> None:
        self.count += 1
        if self.first is None:
            self.first = m

        raw_tokens = m.get("clobTokenIds")
        if raw_tokens:
            self.with_prices += 1
            if len(self.price_candidates) < self._want and _parse_tokens(raw_tokens):
                # Only retain candidates that actually parse, so the pool
                # matches what the sampler would really have used.
                self.price_candidates.append(m)

        if m.get("description") or m.get("resolutionSource"):
            self.with_rules += 1

        v = m.get("endDate") or m.get("end_date")
        if isinstance(v, str) and v:
            if self.earliest is None or v < self.earliest:
                self.earliest = v
            if self.latest is None or v > self.latest:
                self.latest = v

    def time_range(self) -> dict:
        return {"earliest_endDate": self.earliest, "latest_endDate": self.latest}


def _parse_tokens(raw):
    """Decode clobTokenIds, tolerating the string-encoded form. None if unusable."""
    try:
        toks = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return None
    return toks or None


def _replay_snapshots(upto_page: int, stats: MarketStats
                      ) -> tuple[list[dict], set]:
    """Rebuild collected state from raw snapshots already on disk.

    Snapshots are immutable and page-indexed, so a resumed run reconstructs
    exactly the aggregate a single uninterrupted run would have held. Records
    are folded into `stats` page by page and then released, so replaying a
    3,000-page corpus costs one page of memory, not the whole corpus.
    """
    snapshots: list[dict] = []
    seen_ids: set = set()
    for page in range(upto_page + 1):
        path = _page_path(page)
        if not path.exists():
            break
        body = path.read_bytes()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            break
        batch = (payload.get("markets") if isinstance(payload, dict) else payload) or []
        snapshots.append({
            "file": str(path), "sha256": _sha256(body),
            "url": "(replayed from disk)", "records": len(batch),
        })
        for m in batch:
            if m.get("id") not in seen_ids:
                seen_ids.add(m.get("id"))
                stats.add(m)
    return snapshots, seen_ids


def _save_checkpoint(page: int, cursor: str | None, collected: int) -> None:
    CHECKPOINT_PATH.write_text(json.dumps(
        {"next_page": page, "cursor": cursor, "collected": collected}))


def collect_markets(limit: int, max_pages: int | None, resume: bool = False,
                    price_samples: int = 0) -> tuple[MarketStats, dict]:
    """Page the whole closed-market corpus via keyset pagination.

    Returns (stats, state) where stats is a streaming aggregate and state
    carries completion/stop info for the manifest. Never raises for fetch
    failures — a partial result plus an honest `stopped_reason` beats an
    exception that loses the pages already on disk.

    Records are written to disk and folded into `stats`, never retained: the
    corpus is >1.3M markets and holding it would exhaust any reasonable box.

    A checkpoint is written after every page, so an interrupted run (SIGKILL,
    dropped connection, closed laptop) resumes with --resume instead of
    re-pulling hundreds of pages.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    snapshots: list[dict] = []
    stats = MarketStats(price_samples)
    seen_ids: set = set()

    cursor: str | None = None
    seen_cursors: set[str] = set()
    page = 0
    duplicates = 0
    complete = False
    reason = "unknown"

    if resume and CHECKPOINT_PATH.exists():
        ck = json.loads(CHECKPOINT_PATH.read_text())
        page, cursor = ck["next_page"], ck["cursor"]
        snapshots, seen_ids = _replay_snapshots(page - 1, stats)
        print(f"resuming at page {page} with {stats.count} markets replayed from disk")
        if cursor:
            seen_cursors.add(cursor)

    try:
        while True:
            if max_pages is not None and page >= max_pages:
                reason = f"reached --max-pages={max_pages}"
                print(reason)
                break

            url = _keyset_url(limit, cursor)
            body = _get(url)

            out = _page_path(page)
            out.write_bytes(body)  # immutable raw snapshot, exactly as returned

            try:
                payload = json.loads(body)
            except json.JSONDecodeError as e:
                reason = f"non-JSON response on page {page}: {e}"
                print(f"  ! {reason}", file=sys.stderr)
                break

            batch = payload.get("markets") if isinstance(payload, dict) else payload
            batch = batch or []
            next_cursor = payload.get("next_cursor") if isinstance(payload, dict) else None

            snapshots.append({
                "file": str(out),
                "sha256": _sha256(body),
                "url": url,
                "records": len(batch),
            })

            # Trap 2: an ignored/echoed cursor silently re-serves the same page.
            new_count = 0
            for m in batch:
                if m.get("id") in seen_ids:
                    continue
                seen_ids.add(m.get("id"))
                stats.add(m)
                new_count += 1
            duplicates += len(batch) - new_count
            # `batch` goes out of scope with the loop — nothing is retained.
            new = new_count

            print(f"page {page} n={len(batch)} new={new_count} total={stats.count}")

            if not batch:
                complete, reason = True, "empty page — end of closed markets"
                print(reason)
                break
            if not next_cursor:
                complete, reason = True, "no next_cursor — end of closed markets"
                print(reason)
                break
            if not new:
                reason = f"cursor stopped advancing at page {page} (all ids already seen)"
                print(f"  ! {reason}", file=sys.stderr)
                break
            if next_cursor in seen_cursors:
                reason = f"cursor repeated at page {page} — refusing to loop"
                print(f"  ! {reason}", file=sys.stderr)
                break

            seen_cursors.add(next_cursor)
            cursor = next_cursor
            page += 1
            # Checkpoint AFTER the page is durably on disk, so a resume never
            # skips a page it only partially wrote.
            _save_checkpoint(page, cursor, stats.count)

    except FetchError as e:
        reason = f"fetch failed: {e}"
        print(f"  ! {reason}", file=sys.stderr)
    except KeyboardInterrupt:
        reason = "interrupted by user (Ctrl-C)"
        print(f"\n  ! {reason}", file=sys.stderr)

    state = {
        "complete": complete,
        "stopped_reason": reason,
        "pages_fetched": len(snapshots),
        "duplicate_records_skipped": duplicates,
        "snapshots": snapshots,
    }
    return stats, state


def sample_price_history(candidates: list[dict], n: int) -> dict:
    """Grab price history for the first n markets that expose CLOB token ids.

    `candidates` is the bounded pool MarketStats retained during collection —
    already filtered to markets whose clobTokenIds parse — rather than the
    whole corpus.
    """
    result = {"requested": n, "collected": 0, "failed": 0}
    if n <= 0:
        return result
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    got = 0
    failed = 0
    for m in candidates:
        if got >= n:
            break
        token_ids = _parse_tokens(m.get("clobTokenIds"))
        if not token_ids:
            continue
        token = token_ids[0]
        url = f"{CLOB}/prices-history?market={token}&interval=max&fidelity=60"
        try:
            body = _get(url, tries=3)
        except FetchError as e:
            # One market's missing history must not abort the sample.
            failed += 1
            print(f"  ! price history failed for market {m.get('id')}: {e}", file=sys.stderr)
            continue
        mid = m.get("id", "unknown")
        (PRICES_DIR / f"{mid}_{token}.json").write_bytes(body)
        got += 1
        print(f"  price history {got}/{n}: market {mid}")
    print(f"sampled price history for {got} markets ({failed} failed)")
    result.update(collected=got, failed=failed)
    return result


def _write_manifest(state: dict, stats: MarketStats, limit: int, prices: dict | None) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "experiment": "001",
        "stage": "exploratory-collection (Sub 1 / #2) — NOT hardened",
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_endpoints": [
            f"{GAMMA}/markets/keyset?closed=true (after_cursor pagination)",
            f"{CLOB}/prices-history",
        ],
        "pagination": "keyset/after_cursor",
        "page_limit_requested": limit,
        "page_limit_server_cap": PAGE_CAP,
        "complete": state["complete"],
        "stopped_reason": state["stopped_reason"],
        "pages_fetched": state["pages_fetched"],
        "total_markets": stats.count,
        "duplicate_records_skipped": state["duplicate_records_skipped"],
        "price_history": prices,
        "time_range": stats.time_range(),
        "snapshots": state["snapshots"],
        "coverage_note": (
            "Exploratory pull of closed Polymarket markets via the public Gamma "
            "keyset API. `complete` reflects whether pagination reached a natural "
            "end; if false, `stopped_reason` says where it stopped and the corpus "
            "is a prefix, not a sample. Verify total_markets against the >=5,000 / "
            "full-coverage requirement in #2 before treating this as the corpus."
        ),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"wrote manifest -> {MANIFEST_PATH} (complete={state['complete']})")


def _peek(stats: MarketStats) -> None:
    """Print what's actually there so a human can eyeball the shape."""
    print("\n===== WHAT'S THERE =====")
    print(f"total closed markets collected: {stats.count}")
    if not stats.count or stats.first is None:
        return
    first = stats.first
    print(f"\nfield keys on first market ({len(first)} fields):")
    for k in sorted(first):
        val = first[k]
        preview = str(val)
        if len(preview) > 80:
            preview = preview[:77] + "..."
        print(f"  {k}: {preview}")
    print(f"\nmarkets exposing clobTokenIds (price-history capable): {stats.with_prices}")
    print(f"markets with description/resolutionSource text: {stats.with_rules}")
    print(f"rough endDate range: {stats.time_range()}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Exploratory Polymarket closed-market collector.")
    ap.add_argument("--limit", type=int, default=PAGE_CAP,
                    help=f"page size (server caps at {PAGE_CAP})")
    ap.add_argument("--max-pages", type=int, default=None, help="cap pages (smoke test)")
    ap.add_argument("--price-samples", type=int, default=10,
                    help="how many markets to pull price history for (0 to skip)")
    ap.add_argument("--resume", action="store_true",
                    help="continue from the last checkpoint instead of restarting")
    args = ap.parse_args()

    if args.limit > PAGE_CAP:
        print(f"note: --limit {args.limit} exceeds the server cap; using {PAGE_CAP}")
        args.limit = PAGE_CAP

    stats, state = collect_markets(limit=args.limit, max_pages=args.max_pages,
                                   resume=args.resume,
                                   price_samples=args.price_samples)

    prices = None
    try:
        prices = sample_price_history(stats.price_candidates, args.price_samples)
    except KeyboardInterrupt:
        print("\n  ! price sampling interrupted", file=sys.stderr)
    finally:
        # Written no matter how we got here, so a partial pull is never lost
        # or — worse — described by a stale manifest from an earlier run.
        _write_manifest(state, stats, args.limit, prices)

    _peek(stats)
    print("\ndone. raw snapshots under data/raw/polymarket/ (gitignored),"
          f" manifest at {MANIFEST_PATH} (tracked).")
    if not state["complete"]:
        print(f"WARNING: pull is INCOMPLETE — {state['stopped_reason']}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
