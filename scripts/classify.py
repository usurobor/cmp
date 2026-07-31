#!/usr/bin/env python3
"""classify.py — small heuristics and pydantic schemas for labeling."""

from pathlib import Path

import polars as pl
import typer
from pydantic import BaseModel

app = typer.Typer()


class Market(BaseModel):
    id: str
    question: str
    source: str | None = None
    question_norm: str | None = None


@app.command()
def run(source: str = "data/normalized.parquet") -> None:
    """Load normalized markets into pydantic models and report a trivial label count."""
    path = Path(source)
    if not path.exists():
        typer.echo(f"No normalized data at {source}")
        raise typer.Exit(code=1)

    markets = [Market(**row) for row in pl.read_parquet(path).to_dicts()]
    conditional = sum(1 for m in markets if " if " in (m.question_norm or m.question.lower()))
    typer.echo(f"{len(markets)} markets, {conditional} look conditional")


if __name__ == "__main__":
    app()
