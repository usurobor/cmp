#!/usr/bin/env python3
"""collect.py — aggregate and deduplicate fetched market data."""

from pathlib import Path

import polars as pl
import typer

app = typer.Typer()

READERS = {".parquet": pl.read_parquet, ".csv": pl.read_csv, ".json": pl.read_json}


@app.command()
def run(raw_dir: str = "data/raw", out: str = "data/collected.parquet") -> None:
    raw = Path(raw_dir)
    if not raw.exists():
        typer.echo(f"No raw data directory at {raw_dir}")
        raise typer.Exit(code=1)

    frames = [READERS[p.suffix.lower()](p) for p in sorted(raw.glob("**/*")) if p.suffix.lower() in READERS]
    if not frames:
        typer.echo("No files found to collect.")
        raise typer.Exit(code=0)

    df = pl.concat(frames, how="diagonal_relaxed")
    if "id" in df.columns:
        df = df.unique(subset=["id"], keep="first")

    Path(out).parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out)
    typer.echo(f"Wrote {df.height} rows -> {out}")


if __name__ == "__main__":
    app()
