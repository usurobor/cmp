# Experiment 001 — Sub 1 collection (exploratory stage)

Status: **exploration / manual**, not a hardened system. Tracking issue: #2 (sub of #1).

This stage exists to *get the data and see what's there*. The collector
(`scripts/explore_polymarket.py`) is deliberately quick-and-dirty: stdlib-only,
no typer/pydantic/tests, no content-addressed store. It will be rewritten once we
know the real Polymarket field shapes. Do not mistake it for the hardened
collector the #2 contract specifies (typed schema, immutable content-addressed
snapshots, pytest fixtures) — that's a later pass.

## Run it

Requires outbound network to `*.polymarket.com`.

```bash
python3 scripts/explore_polymarket.py --max-pages 4 --price-samples 5   # quick smoke test
python3 scripts/explore_polymarket.py                                   # full closed-market pull
```

Outputs:
- `data/raw/polymarket/markets_off*.json` — raw Gamma API pages, verbatim (gitignored).
- `data/raw/polymarket/prices/*.json` — sampled CLOB price histories (gitignored).
- `docs/experiment-001/manifest.json` — counts, endpoints, per-file sha256, rough
  time range, coverage note (tracked; commit this + the console "WHAT'S THERE" summary).

## ⚠ Egress note (Claude Code web sandbox)

The Claude Code **web** environment's network policy blocks general external
hosts — only package registries (pypi, npm, …) and GitHub are reachable, so
`gamma-api.polymarket.com` returns a 403 policy denial from the egress proxy.
The collector therefore **cannot run inside the web sandbox**. Run it where
egress is open (a local machine, or a web environment configured with a network
policy that allowlists `gamma-api.polymarket.com` and `clob.polymarket.com`).

## Data source (public Polymarket APIs)

- Gamma markets: `https://gamma-api.polymarket.com/markets?closed=true&limit=500&offset=N`
  — market metadata: `question`, `description`, `outcomes`, `outcomePrices`,
  `volume`, `liquidity`, `startDate`/`endDate`, `clobTokenIds`, `resolutionSource`, …
  (exact fields confirmed by the `WHAT'S THERE` summary once you run it).
- CLOB price history: `https://clob.polymarket.com/prices-history?market=<clobTokenId>&interval=max&fidelity=60`.
