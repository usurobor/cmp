#!/usr/bin/env python3
"""normalize.py — lightweight text normalization helpers."""

import re
import unicodedata
from pathlib import Path

import polars as pl
import typer

app = typer.Typer()

_WS = re.compile(r"\s+")


def normalize_text(s: str | None) -> str:
    """NFKC-normalize, collapse whitespace, lowercase."""
    if not s:
        return ""
    return _WS.sub(" ", unicodedata.normalize("NFKC", s)).strip().lower()


@app.command()
def run(source: str = "data/collected.parquet", out: str = "data/normalized.parquet") -> None:
    df = pl.read_parquet(source)
    if "question" in df.columns:
        df = df.with_columns(
            pl.col("question")
            .map_elements(normalize_text, return_dtype=pl.String)
            .alias("question_norm")
        )
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out)
    typer.echo(f"Normalized {df.height} rows -> {out}")


if __name__ == "__main__":
    app()
