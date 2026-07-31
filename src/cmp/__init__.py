"""cmp — Coherent Markets research package."""

__all__ = ["__version__", "say_hello"]
__version__ = "0.1.0"


def say_hello() -> str:
    return f"cmp {__version__} — minimal research package"
