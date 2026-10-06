"""Asynchronous Watts threshold cascade and blocked-channel comparison (C3)."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

from ._inference import bootstrap_ci, finite, sample_variance


def threshold_cascade(
    nodes: Sequence[str], edges: Sequence[tuple[str, str]],
    thresholds: Mapping[str, float], seed_nodes: Sequence[str], *,
    blocked_channels: Sequence[tuple[str, str]] = (),
    normalization: str = "modified_degree", seed: int = 0,
    max_sweeps: int = 10_000,
) -> dict:
    """Random asynchronous activation; blocked channels have two explicit semantics.

    ``modified_degree`` removes a blocked edge from numerator and denominator.
    ``original_degree`` removes only its signal. Duration is in sweeps, not seconds.
    """
    members = list(nodes)
    if len(set(members)) != len(members) or not members or max_sweeps < 1:
        raise ValueError("nodes must be nonempty and unique; max_sweeps positive")
    if normalization not in {"modified_degree", "original_degree"}:
        raise ValueError("normalization must be modified_degree or original_degree")
    member_set = set(members)
    if not set(seed_nodes) <= member_set or set(thresholds) != member_set:
        raise ValueError("seed nodes and threshold keys must match graph nodes")
    edge_set = set(edges)
    if len(edge_set) != len(edges) or any(a not in member_set or b not in member_set for a, b in edges):
        raise ValueError("edges must be unique and reference graph nodes")
    blocked = set(blocked_channels)
    if not blocked <= edge_set:
        raise ValueError("blocked channels must exist in the graph")
    limits = {node: finite(thresholds[node], "threshold") for node in members}
    if any(not 0 <= value <= 1 for value in limits.values()):
        raise ValueError("thresholds must lie in [0,1]")
    incoming = {node: [] for node in members}
    for source, target in edges:
        incoming[target].append(source)
    active = set(seed_nodes)
    log = [{"node": node, "sweep": 0, "order": 0, "contributors": []} for node in members if node in active]
    rng = random.Random(seed)
    last_activation_sweep = 0
    for sweep in range(1, max_sweeps + 1):
        permutation = members.copy()
        rng.shuffle(permutation)
        changed = False
        for order, node in enumerate(permutation, start=1):
            if node in active:
                continue
            original = incoming[node]
            allowed = [source for source in original if (source, node) not in blocked]
            denominator = len(original) if normalization == "original_degree" else len(allowed)
            # 孤立节点不能靠除零激活；异步更新即时影响同一轮后续节点。
            contributors = [source for source in allowed if source in active]
            if denominator and len(contributors) / denominator >= limits[node]:
                active.add(node)
                log.append({"node": node, "sweep": sweep, "order": order,
                            "contributors": contributors})
                last_activation_sweep = sweep
                changed = True
        if not changed:
            return {"activated": [node for node in members if node in active],
                    "size": len(active), "fraction": len(active) / len(members),
                    "duration_sweeps": last_activation_sweep, "converged": True,
                    "activation_log": log, "normalization": normalization}
    return {"activated": [node for node in members if node in active],
            "size": len(active), "fraction": len(active) / len(members),
            "duration_sweeps": last_activation_sweep, "converged": False,
            "activation_log": log, "normalization": normalization}


def compare_blocked_channels(
    nodes: Sequence[str], edges: Sequence[tuple[str, str]], thresholds: Mapping[str, float],
    seed_nodes: Sequence[str], blocked_channels: Sequence[tuple[str, str]],
    run_seeds: Sequence[int], *, normalization: str = "modified_degree",
    alpha: float = 0.05,
) -> dict:
    """Paired cascade-size effect: full graph minus blocked graph."""
    if len(run_seeds) < 2 or not blocked_channels:
        raise ValueError("need at least two independent seeds and a blocked channel")
    pairs = []
    for seed in run_seeds:
        full = threshold_cascade(nodes, edges, thresholds, seed_nodes,
                                 normalization=normalization, seed=seed)
        blocked = threshold_cascade(nodes, edges, thresholds, seed_nodes,
                                    blocked_channels=blocked_channels,
                                    normalization=normalization, seed=seed)
        if not full["converged"] or not blocked["converged"]:
            raise ValueError("cascade did not converge; increase max_sweeps or inspect graph")
        pairs.append({"seed": seed, "full": full, "blocked": blocked,
                      "size_difference": full["size"] - blocked["size"]})
    differences = [pair["size_difference"] for pair in pairs]
    return {"effect_full_minus_blocked": sum(differences) / len(differences),
            "se": (sample_variance(differences) / len(differences)) ** 0.5,
            "ci": bootstrap_ci(differences, alpha=alpha), "pairs": pairs,
            "note": "Blocking can increase activation when the denominator changes."}
