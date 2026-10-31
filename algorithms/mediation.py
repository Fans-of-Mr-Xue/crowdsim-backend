"""Linear no-interaction causal mediation analysis (method C2)."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

from ._inference import finite, ordinary_least_squares, percentile


def _fit(rows: Sequence[Mapping], treatment: str, mediator: str, outcome: str,
         covariates: Sequence[str]) -> dict:
    a = [finite(row[treatment], treatment) for row in rows]
    if any(value not in (0, 1) for value in a):
        raise ValueError("treatment must be coded 0/1")
    m = [finite(row[mediator], mediator) for row in rows]
    y = [finite(row[outcome], outcome) for row in rows]
    controls = [[finite(row[key], key) for key in covariates] for row in rows]
    mediator_design = [[1.0, exposure, *c] for exposure, c in zip(a, controls)]
    outcome_design = [[1.0, exposure, mediating, *c]
                      for exposure, mediating, c in zip(a, m, controls)]
    # 线性且无 A×M 交互时，间接效应是两段路径系数的乘积。
    alpha = ordinary_least_squares(mediator_design, m)
    beta = ordinary_least_squares(outcome_design, y)
    acme = alpha[1] * beta[2]
    ade = beta[1]
    return {"ACME0": acme, "ACME1": acme, "ADE0": ade, "ADE1": ade,
            "TE": acme + ade, "mediator_coefficients": alpha,
            "outcome_coefficients": beta}


def linear_mediation(
    rows: Sequence[Mapping], *, treatment: str, mediator: str,
    outcome: str, covariates: Sequence[str] = (),
    mediator_precedes_outcome: bool, assumptions_accepted: bool,
    alpha: float = 0.05, bootstrap_repetitions: int = 1000,
    seed: int = 0,
) -> dict:
    """Fit linear mediator/outcome models under explicit identifying assumptions.

    Requires no A×M interaction. This estimates model-based natural effects
    only if treatment and mediator sequential ignorability is defensible.
    """
    if not mediator_precedes_outcome or not assumptions_accepted:
        raise ValueError("temporal order and mediation assumptions must be explicitly affirmed")
    if len(rows) < len(covariates) + 6 or bootstrap_repetitions < 100 or not 0 < alpha < 1:
        raise ValueError("insufficient independent runs or invalid bootstrap configuration")
    estimate = _fit(rows, treatment, mediator, outcome, covariates)
    rng = random.Random(seed)
    draws = {key: [] for key in ("ACME0", "ACME1", "ADE0", "ADE1", "TE")}
    attempts = 0
    while len(draws["TE"]) < bootstrap_repetitions and attempts < bootstrap_repetitions * 20:
        attempts += 1
        # 按独立运行行重抽样，并在每次样本上重拟合两个模型。
        sample = rng.choices(rows, k=len(rows))
        try:
            fitted = _fit(sample, treatment, mediator, outcome, covariates)
        except ValueError:
            continue
        for key in draws:
            draws[key].append(fitted[key])
    if len(draws["TE"]) < bootstrap_repetitions:
        raise ValueError("too many singular bootstrap samples; mediation CI is unavailable")
    estimate["ci"] = {key: (percentile(values, alpha / 2), percentile(values, 1 - alpha / 2))
                      for key, values in draws.items()}
    estimate["ci_method"] = "independent-run refit percentile bootstrap"
    estimate["assumptions"] = ["linear mediator and outcome models", "no treatment-mediator interaction",
                               "consistency", "positivity", "sequential ignorability"]
    estimate["warning"] = "Randomized treatment alone does not remove mediator-outcome confounding."
    estimate["unmeasured_confounding_sensitivity"] = None
    return estimate
