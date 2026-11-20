class TrajectoryAnalysisService:
    """从结构化 Trajectory 提取事实层结果。不依赖 LLM。"""

    @staticmethod
    def analyze_group(group_doc: dict) -> dict:
        """对单个 group 的 trajectories 做事实提取。"""
        trajs = group_doc.get("trajectories", [])
        world_count = len(trajs)
        completed = [t for t in trajs if not t.get("error")]
        failed = [t for t in trajs if t.get("error")]
        completed_count = len(completed)
        failed_count = len(failed)

        if not trajs:
            return {
                "world_count": 0, "completed_count": 0, "failed_count": 0,
                "avg_ticks": 0, "exit_rate": 0,
                "outcome_distribution": [],
                "representative_world_ids": [],
                "turning_points": [],
                "metric_series": {},
                "evidence_refs": [],
            }

        # 失败世界单列，不参与业务统计，避免 timeout/异常把均值和终局率拉偏。
        stats_trajs = completed
        tick_counts = [len(t.get("ticks", [])) for t in stats_trajs]
        avg_ticks = round(sum(tick_counts) / len(tick_counts), 1) if tick_counts else 0
        exit_count = 0
        for t in stats_trajs:
            ticks = t.get("ticks", [])
            if ticks and ticks[-1].get("exit"):
                exit_count += 1
        exit_rate = round(exit_count / world_count, 2) if world_count else 0

        # outcome_distribution 只对成功执行的轨迹聚类
        sigs: dict[str, int] = {}
        sig_examples: dict[str, str] = {}
        for t in completed:
            w_id = t.get("world_id", "?")
            finished = t.get("finished", False)
            ticks = t.get("ticks", [])
            last_exit = ticks[-1].get("exit") if ticks else False
            rv = t.get("rv_values") or {}
            rv_str = ",".join(f"{k}={v}" for k, v in sorted(rv.items())[:3]) if rv else "no_rv"
            sig = f"finished={finished}|exit={last_exit}|rv=[{rv_str}]"
            sigs[sig] = sigs.get(sig, 0) + 1
            if sig not in sig_examples:
                sig_examples[sig] = w_id

        total = sum(sigs.values()) or 1
        outcome_dist = [
            {"signature": k, "count": v, "proportion": round(v / total, 2),
             "example_world_id": sig_examples.get(k, "?")}
            for k, v in sorted(sigs.items(), key=lambda x: -x[1])
        ]

        # representative_world_ids
        rep_ids = []
        if completed:
            by_ticks = sorted(completed, key=lambda t: len(t.get("ticks", [])))
            if by_ticks:
                rep_ids.append({"role": "shortest", "world_id": by_ticks[0].get("world_id")})
            if len(by_ticks) > 1:
                rep_ids.append({"role": "longest", "world_id": by_ticks[-1].get("world_id")})
            if len(by_ticks) >= 3:
                mid = by_ticks[len(by_ticks) // 2]
                rep_ids.append({"role": "median", "world_id": mid.get("world_id")})
        if failed:
            rep_ids.append({"role": "failed_example", "world_id": failed[0].get("world_id")})

        # turning_points
        turning_pts = []
        for t in trajs:
            w_id = t.get("world_id", "?")
            ticks = t.get("ticks", [])
            for i in range(1, len(ticks)):
                prev_exit = ticks[i - 1].get("exit", False)
                curr_exit = ticks[i].get("exit", False)
                if prev_exit != curr_exit:
                    turning_pts.append({
                        "group_id": group_doc.get("group_id", "?"),
                        "world_id": w_id, "tick_idx": i,
                        "reason": f"exit 状态变化 {prev_exit}→{curr_exit}",
                    })
                prev_state = ticks[i - 1].get("world_state") or {}
                curr_state = ticks[i].get("world_state") or {}
                changed_state = [k for k in set(prev_state) | set(curr_state)
                                 if prev_state.get(k) != curr_state.get(k)]
                if changed_state:
                    turning_pts.append({
                        "group_id": group_doc.get("group_id", "?"),
                        "world_id": w_id, "tick_idx": i,
                        "reason": f"世界态变化: {', '.join(sorted(changed_state)[:5])}",
                    })
                prev_action = (ticks[i - 1].get("decision") or {}).get("action")
                curr_action = (ticks[i].get("decision") or {}).get("action")
                if prev_action and curr_action and prev_action != curr_action:
                    turning_pts.append({
                        "group_id": group_doc.get("group_id", "?"),
                        "world_id": w_id, "tick_idx": i,
                        "reason": "决策行动变化",
                    })
                rv_snaps = t.get("rv_snapshots") or []
                if i < len(rv_snaps) and i > 0:
                    prev_rv = rv_snaps[i - 1].get("rv_values", {})
                    curr_rv = rv_snaps[i].get("rv_values", {})
                    changed = [k for k in set(list(prev_rv) + list(curr_rv))
                              if prev_rv.get(k) != curr_rv.get(k)]
                    if changed:
                        turning_pts.append({
                            "group_id": group_doc.get("group_id", "?"),
                            "world_id": w_id, "tick_idx": i,
                            "reason": f"RV 变化: {', '.join(changed[:5])}",
                        })
        turning_pts = turning_pts[:20]

        # evidence_refs（可回链到 group/world/tick）
        evidence = []
        for t in trajs[:5]:
            w_id = t.get("world_id", "?")
            ticks = t.get("ticks", [])
            last_tick = ticks[-1].get("tick_idx") if ticks else None
            evidence.append({
                "group_id": group_doc.get("group_id", "?"),
                "world_id": w_id,
                "tick_count": len(ticks),
                "last_tick_idx": last_tick,
                "finished": t.get("finished"),
                "error": t.get("error"),
            })

        # metric_series：从 world_state 和 rv_snapshots 提取数值
        metric_series: dict[str, list[dict]] = {}
        buckets: dict[str, dict[int, list[float]]] = {}
        for t in completed:
            for tick in t.get("ticks", []) or []:
                idx = tick.get("tick_idx")
                for key, value in (tick.get("world_state") or {}).items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        buckets.setdefault(f"world_state.{key}", {}).setdefault(idx, []).append(float(value))
            for snap in t.get("rv_snapshots", []) or []:
                idx = snap.get("tick_idx")
                for key, value in (snap.get("rv_values") or {}).items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        buckets.setdefault(f"rv.{key}", {}).setdefault(idx, []).append(float(value))
        for metric, by_tick in buckets.items():
            metric_series[metric] = [
                {"tick_idx": idx, "mean": round(sum(values) / len(values), 4),
                 "sample_size": len(values)}
                for idx, values in sorted(by_tick.items()) if values
            ]

        return {
            "world_count": world_count,
            "completed_count": completed_count,
            "failed_count": failed_count,
            "avg_ticks": avg_ticks,
            "exit_rate": exit_rate,
            "outcome_distribution": outcome_dist,
            "representative_world_ids": rep_ids,
            "turning_points": turning_pts,
            "metric_series": metric_series,
            "evidence_refs": evidence,
        }

    @staticmethod
    def group_summary_for_analysis(group_doc: dict) -> dict:
        facts = TrajectoryAnalysisService.analyze_group(group_doc)
        return {
            "group_id": group_doc.get("group_id"),
            "kind": group_doc.get("kind"),
            "name": group_doc.get("name"),
            "facts": facts,
        }

    @staticmethod
    def get_numeric_metrics(group_doc: dict) -> dict[str, float]:
        """提取 group 的可计算数值指标（终局 RV 均值等），供 analysis_objectives 使用。"""
        facts = TrajectoryAnalysisService.analyze_group(group_doc)
        metrics: dict[str, float] = {}
        for key, series in facts.get("metric_series", {}).items():
            if series:
                metrics[key] = series[-1].get("mean", 0.0)
        # 基础指标
        metrics["avg_ticks"] = facts.get("avg_ticks", 0.0)
        metrics["exit_rate"] = facts.get("exit_rate", 0.0)
        metrics["completed_count"] = facts.get("completed_count", 0)
        metrics["failed_count"] = facts.get("failed_count", 0)
        metrics["world_count"] = facts.get("world_count", 0)
        return metrics
