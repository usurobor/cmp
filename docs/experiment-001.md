# Experiment 001 — Exploratory protocol

## Purpose

Investigate semantic, logical, and cross-market incoherence in prediction markets. Start by
collecting market question text, outcomes (when available), price histories and metadata, then run
simple classification pipelines over them.

## Research questions

1. How frequently do semantically similar questions produce divergent market probabilities?
2. Are there reproducible patterns in sentence structure or implied assumptions that correlate with
   divergent outcomes?
3. Can simple rule-based normalization resolve some fraction of apparent incoherence?

## Data collection

- Start with a small set of markets collected via public APIs (CSV/JSON).
- Store as Parquet for repeatable, schema-aware analysis.

## Pipeline (minimal)

1. `retrieve.py` — fetch raw market data, write raw JSON/Parquet
2. `collect.py` — aggregate and deduplicate
3. `normalize.py` — canonicalize question text (unicode NFKC, whitespace, casing)
4. `classify.py` — small heuristics and pydantic schemas for labeling
5. `analyze.py` — polars/duckdb experiments, export CSV/Parquet for notebooks

## Evaluation

Track counts of near-duplicates, semantic similarity scores, and disagreement measures across
markets. Log findings in markdown files and small notebooks (outside the scope of this scaffold).

## Ethics & privacy

- Strip or avoid storing any personally identifiable data.
- Respect the API terms of service of any market or data provider.
