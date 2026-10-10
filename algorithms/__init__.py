"""Reusable computations for CrowdSim post-event analysis.

All functions are pure and independent of HTTP, SUMO, and scenario fixtures.
"""

from .causal_graph import (
    compute_leverages,
    enumerate_risk_paths,
    find_hub_nodes,
    simulate_intervention,
)
from .mechanism_graph import compute_key_nodes, format_loops, list_lag_effects
from .metrics import (
    active_population_counts, aggregation_start, arrival_rates, bottleneck_width, compare_metrics,
    evacuation_time, gate_capacity, information_coverage, kernel_crowd_pressure,
    max_contact_pressure, max_kernel_crowd_pressure, mean_movement_speed, normalized_intensity,
    paired_metric_effect, peak_grid_density, population_factors, response_delays,
    weighted_score,
)
from .randomized_simulation import randomized_simulation
from .paired_mc import paired_monte_carlo
from .timing_sensitivity import timing_sensitivity
from .ofat import ofat_scan, local_derivative
from .two_factor import two_factor_effects
from .orthogonal_design import orthogonal_design_analysis
from .mechanism_ablation import mechanism_ablation, ablation_interaction
from .mediation import linear_mediation
from .cascade_path import threshold_cascade, compare_blocked_channels
from .pc import fisher_z_ci, pc_discovery
from .sobol import sobol_indices
from .morris import morris_elementary_effects
from .numeric import (
    best_partial_correlation,
    mean,
    partial_correlation,
    pearson,
    residualize,
    standard_deviation,
)

__all__ = [
    "ablation_interaction",
    "active_population_counts",
    "aggregation_start",
    "arrival_rates",
    "best_partial_correlation",
    "bottleneck_width",
    "compare_blocked_channels",
    "compare_metrics",
    "compute_key_nodes",
    "compute_leverages",
    "enumerate_risk_paths",
    "evacuation_time",
    "fisher_z_ci",
    "find_hub_nodes",
    "format_loops",
    "gate_capacity",
    "information_coverage",
    "kernel_crowd_pressure",
    "linear_mediation",
    "list_lag_effects",
    "local_derivative",
    "max_contact_pressure",
    "max_kernel_crowd_pressure",
    "mean",
    "mean_movement_speed",
    "mechanism_ablation",
    "morris_elementary_effects",
    "normalized_intensity",
    "ofat_scan",
    "orthogonal_design_analysis",
    "paired_metric_effect",
    "paired_monte_carlo",
    "partial_correlation",
    "pc_discovery",
    "pearson",
    "peak_grid_density",
    "population_factors",
    "randomized_simulation",
    "residualize",
    "response_delays",
    "simulate_intervention",
    "sobol_indices",
    "standard_deviation",
    "threshold_cascade",
    "timing_sensitivity",
    "two_factor_effects",
    "weighted_score",
]
