"""Paired mechanism-switch ablation contrasts (method C1)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ._inference import paired_summary
from .two_factor import two_factor_effects


def mechanism_ablation(
    full_runs: Sequence[float], ablated_runs: Mapping[str, Sequence[float]],
    replacement_definitions: Mapping[str, str], *,
    alpha: float = 0.05, seed: int = 0,
) -> dict:
    """Compare each valid replacement with the same full-model run blocks."""
    if not ablated_runs:
        raise ValueError("at least one ablated mechanism is required")
    effects = {}
    for name, runs in ablated_runs.items():
        replacement = replacement_definitions.get(name, "").strip()
        if not replacement:
            raise ValueError(f"mechanism {name} needs a preregistered replacement definition")
        # 消融差定义为 Y(关闭/替代)-Y(完整)，不是现实机制的自动证明。
        effects[name] = {"replacement": replacement,
                         "effect": paired_summary(full_runs, runs, alpha=alpha, seed=seed)}
    return {"effects": effects, "interpretation": "effect within the specified simulator and replacement rules"}


def ablation_interaction(cells: Mapping[tuple[int, int], Sequence[float]], **kwargs) -> dict:
    """Four paired switch states; J = Y11 - Y10 - Y01 + Y00."""
    return two_factor_effects(cells, **kwargs)
