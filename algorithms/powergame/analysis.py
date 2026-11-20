"""Factor contrasts + trace-derived candidate links, with separate score semantics."""

from __future__ import annotations

from collections import defaultdict
from itertools import product
from statistics import mean

from .catalog import FACTORS, NODES, OUTCOMES
from .contract import digest, evidence_item, normalize, scalar_number, typed_key
from .extraction import ExtractionError, RulesExtractor


def _contrasts(data, evidence, catalog):
    groups = defaultdict(list)
    for world in data["worlds"]:
        if world["eligible"]:
            groups[typed_key(world["input"])].append(world)
    edges = []
    for factor in data["factor_order"]:
        levels = data["factor_levels"][factor]
        reference = levels[0]
        others = [key for key in data["factor_order"] if key != factor]
        strata = [dict(zip(others, values)) for values in product(*(data["factor_levels"][key] for key in others))]
        for comparison in levels[1:]:
            for outcome, meta in catalog["outcomes"].items():
                rows, refs, missing = [], set(), 0
                for other in strata:
                    ref_plan, cmp_plan = {**other, factor: reference}, {**other, factor: comparison}
                    samples = []
                    for plan in (ref_plan, cmp_plan):
                        values = []
                        for world in groups.get(typed_key(plan), []):
                            raw_value = world["trajectory"].get("rv_values", {}).get(meta.get("source_key", outcome))
                            from .dynamic_catalog import metric_value
                            value = metric_value(raw_value, meta)
                            if value is not None:
                                values.append((value, world))
                        samples.append(values)
                    if not all(samples):
                        missing += 1
                        continue
                    ref_mean, cmp_mean = (mean(value for value, _ in values) for values in samples)
                    row_refs = []
                    for values in samples:
                        for _, world in values:
                            item = evidence_item(world, None, f"rv_values.{meta.get('source_key', outcome)}")
                            evidence[item["id"]] = item
                            refs.add(item["id"])
                            row_refs.append(item["id"])
                    rows.append({"other_factors": other, "reference_mean": round(ref_mean, 6),
                                 "comparison_mean": round(cmp_mean, 6), "difference": round(cmp_mean - ref_mean, 6),
                                 "reference_n": len(samples[0]), "comparison_n": len(samples[1]),
                                 "evidence_ids": row_refs})
                score = round(mean(row["difference"] for row in rows), 6) if rows else None
                edges.append({"id": "contrast-" + digest([factor, reference, comparison, outcome])[:16],
                              "source": catalog["factors"].get(factor, {}).get("node_id", factor), "target": meta.get("node_id", outcome), "kind": "level_contrast",
                              "method": "equal_weight_conditional_mean_difference", "score": score,
                              "score_label": "条件等权均值差", "unit": meta["unit"],
                              "reference_level": reference, "comparison_level": comparison,
                              "relation": "promotes" if score is not None and score > 0 else ("inhibits" if score is not None and score < 0 else "neutral"),
                              "status": "complete" if rows and not missing else ("partial" if rows else "insufficient"),
                              "weighting": "对其他因素的完整可比组合等权；缺失不填零",
                              "missing_strata": missing, "strata": rows, "evidence_ids": sorted(refs),
                              "explanation": f"{catalog['factors'].get(factor, {}).get('label', factor)}从{reference}变为{comparison}，比较{meta['label']}。"})
    return edges


def _paths(edges, successful_worlds, outcome_ids=None):
    outcome_ids = set(OUTCOMES) if outcome_ids is None else outcome_ids
    adjacency = defaultdict(list)
    for edge in edges:
        adjacency[edge["source"]].append(edge)
    paths = []

    def visit(node, nodes, trail, common):
        if node in outcome_ids and len(trail) >= 2:
            if common:
                paths.append({"id": "path-" + digest([edge["id"] for edge in trail])[:16],
                              "node_ids": nodes, "edge_ids": [edge["id"] for edge in trail],
                              "support_count": len(common), "denominator": successful_worlds,
                              "mention_ratio": len(common) / successful_worlds,
                              "world_refs": sorted(common), "label": "同一世界共同提及的候选链"})
            return
        if len(trail) >= 4:
            return
        for edge in adjacency[node]:
            if edge["target"] not in nodes:
                intersection = set(edge["world_refs"]) if common is None else common & set(edge["world_refs"])
                if intersection:
                    visit(edge["target"], nodes + [edge["target"]], trail + [edge], intersection)
    for node in {edge["source"] for edge in edges}:
        visit(node, [node], [], None)
    return sorted(paths, key=lambda item: (-item["support_count"], -len(item["edge_ids"]), item["id"]))[:12]


def analyze(payload: dict, extractor=None, *, synthetic=False, progress=None, catalog=None) -> dict:
    data = normalize(payload)
    extractor = extractor or RulesExtractor()
    catalog = catalog or {"factors": FACTORS, "outcomes": OUTCOMES, "nodes": NODES}
    evidence, grouped, world_results = {}, {}, []
    successes = 0
    for world in data["worlds"]:
        item = {key: world[key] for key in ("world_ref", "group_index", "world_index", "input")}
        if not world["eligible"]:
            item.update(status="excluded", error=world["exclusion_reason"], attempts=[])
        else:
            try:
                extracted = extractor.extract(world)
                successes += 1
                item.update(status="success", error=None, attempts=extracted["attempts"],
                            relation_count=len(extracted["relations"]))
                for relation in extracted["relations"]:
                    key = (relation["source"], relation["target"], relation["relation"])
                    if key not in grouped:
                        grouped[key] = {"id": "link-" + digest(key)[:16], "source": key[0], "target": key[1],
                                        "relation": key[2], "kind": "mechanism_candidate", "method": extracted["method"],
                                        "world_refs": set(), "evidence_ids": set(), "explanations": []}
                    edge = grouped[key]
                    edge["world_refs"].add(world["world_ref"])
                    for ev in relation["evidence_items"]:
                        evidence[ev["id"]] = ev
                        edge["evidence_ids"].add(ev["id"])
                    edge["explanations"].append({"world_ref": world["world_ref"], "text": relation["explanation"]})
            except ExtractionError as exc:
                item.update(status="failed", error=str(exc), attempts=exc.attempts)
        world_results.append(item)
        if progress:
            progress({"done": len(world_results), "total": len(data["worlds"]), "status": item["status"]})
    candidates = []
    for value in grouped.values():
        support = len(value["world_refs"])
        candidates.append({**value, "world_refs": sorted(value["world_refs"]), "evidence_ids": sorted(value["evidence_ids"]),
                           "support_count": support, "denominator": successes, "score": support / successes,
                           "score_label": "成功分析世界的提及率", "unit": "ratio", "status": "candidate"})
    candidates.sort(key=lambda item: (-item["support_count"], item["id"]))
    contrasts = _contrasts(data, evidence, catalog)
    failures = sum(item["status"] == "failed" for item in world_results)
    excluded = sum(item["status"] == "excluded" for item in world_results)
    incomplete_contrasts = any(edge["status"] != "complete" for edge in contrasts)
    status = "failed" if not candidates else ("partial" if failures or excluded or data["missing_groups"] or incomplete_contrasts else "complete")
    used = {edge[key] for edge in candidates + contrasts for key in ("source", "target")}
    return {"schema_version": 1, "success": bool(candidates), "status": status,
            "source": {"run_id": data["run_id"], "design_id": data["design_id"], "synthetic": synthetic,
                       "input_sha256": data["input_sha256"]},
            "method": {"mode": extractor.mode, "candidate_score": "distinct_support_worlds / successfully_analyzed_worlds",
                       "contrast_score": "equal-weight mean of within-other-factor level differences",
                       "causal_validation": False, "fallback_used": False},
            "counts": {"groups": data["groups"], "expected_groups": data["expected_groups"],
                       "missing_groups": data["missing_groups"], "worlds": len(world_results), "ticks": data["ticks"],
                       "analyzed_worlds": successes, "failed_worlds": failures, "excluded_worlds": excluded,
                       "candidate_edges": len(candidates), "contrast_edges": len(contrasts)},
            "nodes": [node.copy() for node in catalog["nodes"] if node["id"] in used], "edges": candidates + contrasts,
            "paths": _paths(candidates, successes, {meta.get("node_id", key) for key, meta in catalog["outcomes"].items()}), "evidence_records": evidence,
            "world_results": world_results,
            "limitations": ["候选机制为文本标注或规则匹配，并非已验证因果机制。", "均值差单位为分；提及率不是置信度。",
                            "提及率仅以成功分析世界为分母，必须同时查看失败与排除数量。"]}
