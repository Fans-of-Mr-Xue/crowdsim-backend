"""Pick-freeze first-order and total Sobol sensitivity indices (optional A3)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ._inference import finite, sample_variance


def sobol_indices(
    output_a: Sequence[float], output_b: Sequence[float],
    output_a_bj: Mapping[str, Sequence[float]],
) -> dict[str, dict]:
    """Estimate indices from independent-input Saltelli A, B and A_Bj designs.

    Caller must evaluate a deterministic response or a converged mean over
    simulator randomness. Correlated input factors invalidate this estimator.
    """
    a = [finite(value, "A") for value in output_a]
    b = [finite(value, "B") for value in output_b]
    if len(a) != len(b) or len(a) < 2:
        raise ValueError("A and B need equal length and at least two independent rows")
    variance = sample_variance([*a, *b])
    if variance <= 0:
        raise ValueError("response variance must be positive")
    results = {}
    for factor, hybrid_values in output_a_bj.items():
        hybrid = [finite(value, factor) for value in hybrid_values]
        if len(hybrid) != len(a):
            raise ValueError("each A_Bj output must align with A and B")
        # 一阶指数与总效应指数采用独立输入的 pick-freeze 蒙特卡洛估计。
        first = sum(bi * (abi - ai) for ai, bi, abi in zip(a, b, hybrid)) / len(a) / variance
        total = sum((ai - abi) ** 2 for ai, abi in zip(a, hybrid)) / (2 * len(a) * variance)
        results[factor] = {"first_order": first, "total_effect": total}
    return results
