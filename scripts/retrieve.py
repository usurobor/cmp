#!/usr/bin/env python3
"""retrieve.py — minimal market retrieval stubs."""

import json
from pathlib import Path

import typer

app = typer.Typer()


@app.command()
def run(out: str = "data/raw/sample.json") -> None:
    """Write a small development sample. Replace with real API calls via httpx."""
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    sample = [
        {"id": "mkt-1", "question": "Will event X occur by 2027?", "source": "example"},
        {"id": "mkt-2", "question": "Will event X happen by 2027?", "source": "example"},
    ]
    path.write_text(json.dumps(sample, indent=2))
    typer.echo(f"Wrote sample raw data -> {out}")


if __name__ == "__main__":
    app()
