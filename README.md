# cmp

Coherent Markets — research into semantic, logical, and cross-market incoherence in prediction markets.

This repository is intentionally minimal: a research-first scaffold for data collection, normalization, retrieval, classification, and analysis.

## Tech stack

- Python 3.12+
- polars
- duckdb
- pyarrow (Parquet)
- pydantic
- httpx
- anthropic (Anthropic SDK)
- typer
- pytest
- ruff
- pyright

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e '.[dev]'
pytest
```

## Pipeline

```bash
python scripts/retrieve.py     # fetch raw market data -> data/raw/
python scripts/collect.py      # aggregate + dedupe -> data/collected.parquet
python scripts/normalize.py    # canonicalize question text -> data/normalized.parquet
python scripts/classify.py     # heuristics + pydantic schemas
python scripts/analyze.py      # polars/duckdb experiments
```

## Notes

- Research scaffold, not production software. Keep scripts lightweight and experiment-focused.
- No license included by request.
