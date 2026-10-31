"""Small PC-Stable skeleton with Gaussian Fisher-Z CI tests and collider marks.

Discovery uses independent F-00 seeds only, never pooled paired world-lines.
Undirected edges are retained when orientation is not identified.
"""
from itertools import combinations
from math import atanh, sqrt
from statistics import NormalDist, mean


def inverse(matrix):
    size = len(matrix)
    augmented = [list(row) + [float(index == column) for column in range(size)] for index, row in enumerate(matrix)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda index: abs(augmented[index][column]))
        if abs(augmented[pivot][column]) < 1e-10:
            raise ValueError("Singular covariance")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(size):
            if row != column:
                scale = augmented[row][column]
                augmented[row] = [left - scale * right for left, right in zip(augmented[row], augmented[column])]
    return [row[size:] for row in augmented]


def discover(features, runs):
    nodes = [{"id": feature["id"], "metricId": feature["metricId"], "regionId": feature["regionId"],
              "windowStartSeconds": feature["windowStartSeconds"], "windowEndSeconds": feature["windowEndSeconds"]} for feature in features]
    rows = [run["featureValues"] for run in runs if run["planId"] == "F-00" and run.get("status") == "completed"
            and all(isinstance(run.get("featureValues", {}).get(node["id"]), (int, float)) for node in nodes)]
    if not 2 <= len(nodes) <= 8 or len(rows) < max(20, len(nodes) + 4):
        return {"status": "insufficient_data", "nodes": nodes, "edges": [], "sampleCount": len(rows),
                "reason": "PC-Stable 使用独立 F-00 种子，至少20个完整样本，支持2—8个特征；配对世界线不当作独立样本。"}
    ids = [node["id"] for node in nodes]
    n = len(rows)
    columns = [[row[key] for row in rows] for key in ids]
    centers = [mean(column) for column in columns]
    covariance = [[sum((x - centers[i]) * (y - centers[j]) for x, y in zip(columns[i], columns[j])) / (n - 1)
                   for j in range(len(ids))] for i in range(len(ids))]
    if any(covariance[i][i] < 1e-10 for i in range(len(ids))):
        return {"status": "degenerate_data", "nodes": nodes, "edges": [], "sampleCount": n,
                "reason": "至少一个特征在独立种子间无方差，无法执行 Fisher-Z 条件独立检验。"}
    adjacency = {i: set(range(len(ids))) - {i} for i in range(len(ids))}
    separators, tests, singular = {}, [], 0
    critical = NormalDist().inv_cdf(0.975)
    for depth in range(len(ids) - 1):
        frozen = {key: set(value) for key, value in adjacency.items()}
        removed, eligible = [], False
        for left in range(len(ids)):
            for right in sorted(frozen[left]):
                if left >= right:
                    continue
                candidates = set(combinations(sorted(frozen[left] - {right}), depth))
                candidates.update(combinations(sorted(frozen[right] - {left}), depth))
                if candidates:
                    eligible = True
                for conditions in sorted(candidates):
                    selected = [left, right, *conditions]
                    try:
                        precision = inverse([[covariance[i][j] for j in selected] for i in selected])
                        correlation = -precision[0][1] / sqrt(precision[0][0] * precision[1][1])
                        statistic = sqrt(n - len(conditions) - 3) * abs(atanh(max(-0.999999999, min(0.999999999, correlation))))
                    except (ValueError, ZeroDivisionError):
                        singular += 1
                        continue
                    independent = statistic < critical
                    tests.append({"left": ids[left], "right": ids[right], "conditions": [ids[i] for i in conditions],
                                  "statistic": statistic, "independent": independent})
                    if independent:
                        removed.append((left, right, conditions))
                        break
        for left, right, conditions in removed:
            adjacency[left].discard(right)
            adjacency[right].discard(left)
            separators[frozenset((left, right))] = set(conditions)
        if not eligible:
            break
    arrows = set()
    for center in adjacency:
        for left, right in combinations(sorted(adjacency[center]), 2):
            if right not in adjacency[left] and center not in separators.get(frozenset((left, right)), set()):
                arrows.update(((left, center), (right, center)))
    edges = []
    for left in adjacency:
        for right in sorted(adjacency[left]):
            if left >= right:
                continue
            forward, backward = (left, right) in arrows, (right, left) in arrows
            source, target = (right, left) if backward and not forward else (left, right)
            edges.append({"source": ids[source], "target": ids[target],
                          "orientation": "collider" if forward != backward else "undirected"})
    return {"status": "partially_estimable" if singular else "computed", "algorithm": "PC-Stable skeleton + unshielded colliders",
            "reason": "部分条件集协方差奇异，保留未识别关系，不据此补造独立性或方向。" if singular else None,
            "nodes": nodes, "edges": edges, "sampleCount": n, "sampleUnit": "independent baseline seed",
            "alpha": 0.05, "tests": tests, "singularTestCount": singular,
            "assumptions": ["线性高斯、因果充分性、忠实性；不可将候选图解释为历史真实因果图",
                            "保留未识别方向；未通过 Meek 规则补全方向"]}
