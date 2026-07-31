#!/usr/bin/env python3
"""analyze.py — quick polars/duckdb experiments."""

from pathlib import Path

import polars as pl
import typer

app = typer.Typer()


@app.command()
def run(source: str = "data/normalized.parquet") -> None:
    path = Path(source)
    if not path.exists():
        typer.echo(f"No normalized data at {source}")
        raise typer.Exit(code=1)

    df = pl.read_parquet(path)
    typer.echo(f"Loaded {df.height} rows")
    typer.echo(str(df.schema))

    if "question_norm" in df.columns:
        dupes = df.group_by("question_norm").len().filter(pl.col("len") > 1)
        typer.echo(f"{dupes.height} exact-normalized duplicate groups")


if __name__ == "__main__":
    app()
