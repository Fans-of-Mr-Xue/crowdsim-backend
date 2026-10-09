"""Prompt templates shared by data-service business modules."""
from functools import lru_cache
from pathlib import Path


@lru_cache
def load_prompt(name):
    if name not in {f"{prefix}_{kind}" for prefix in ("emergency_plan", "regulation") for kind in ("extract", "merge", "repair")}:
        raise ValueError("Unknown data-service prompt")
    return (Path(__file__).parent / f"{name}.md").read_text(encoding="utf-8")
