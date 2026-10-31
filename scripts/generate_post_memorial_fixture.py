"""Normalize the supplied synthetic Markdown without inventing measurements."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path


def normalize(source: Path) -> dict:
    content = source.read_text(encoding="utf-8-sig")
    rows, records, residuals = [], [], []
    previous, state, strategy = {}, "", ""
    anchor = datetime(2014, 12, 31, 20)
    for line_number, line in enumerate(content.splitlines(), 1):
        if not line.startswith("| 201"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 14:
            raise ValueError(f"Unexpected source columns at line {line_number}")
        clock = datetime.strptime(cells[0], "%Y-%m-%d %H:%M")
        state, strategy = cells[1] or state, cells[2] or strategy
        region = cells[3][0]
        population, entered, exited, *percentages = map(int, cells[4:])
        population, entered, exited = (round(value * 300 / 800) for value in (population, entered, exited))
        if sum(percentages[:3]) != 100 or sum(percentages[3:]) != 100:
            raise ValueError(f"Invalid demographic percentages at line {line_number}")
        seconds = int((clock - anchor).total_seconds())
        residual = population - (previous.get(region, 0) + entered - exited)
        if residual:
            residuals.append({"sourceLine": line_number, "regionId": region,
                              "timeSeconds": seconds, "populationResidual": residual})
        previous[region] = population
        rows.append({"timeSeconds": seconds, "regionId": region, "population": population,
                     "density": None, "meanSpeed": None, "pressureProxy": None,
                     "sourceTime": clock.isoformat(timespec="minutes") + "+08:00", "state": state})
        records.append({"sourceLine": line_number, "timeSeconds": seconds, "regionId": region,
                        "population": population, "intervalEntered": entered, "intervalExited": exited,
                        "agePercent": dict(zip(("elderly", "teenager", "other"), percentages[:3])),
                        "nationalityPercent": dict(zip(("China", "Korea", "Russia", "other"), percentages[3:])),
                        "strategy": strategy, "populationResidual": residual})
    if not rows:
        raise ValueError("No source records found")
    return {
        "name": "上海纪念塔事后完整测试基线（原始合成数据规范化）",
        "sourceKind": "synthetic_reference",
        "sourceNote": "合成案例按原数据等比例缩放至同一时刻总人数峰值300人，人数及区间进出流量四舍五入为整数；人口占比保留原值。非真实事故监测记录。密度、速度、压力原文缺失，保留 null，由 SUMO 运行测量；取整后的收支残差如实记录。",
        "observations": rows,
        "metadata": {
            "schemaVersion": "post-dataset/2.0", "location": "上海市人民英雄纪念塔",
            "timezone": "Asia/Shanghai", "startTime": "2014-12-31T20:00:00+08:00",
            "durationSeconds": max(row["timeSeconds"] for row in rows),
            "sourceFile": source.name, "sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "synthetic": True, "sourceRecords": records,
            "executionProfile": {
                "scenarioId": "memorial-post-test", "mappingVersion": "memorial-test-map-v1",
                "regionMapping": {"A": {"label": "M1", "edgeId": "178411801#0"},
                                  "B": {"label": "M2", "edgeId": "177931018#0"},
                                  "C": {"label": "M3", "edgeId": "monument_m3"}},
                "mappingNote": "经用户确认的工程测试映射；M3是西侧中部工程假设入口，不是真实北侧通道。",
                "baselineControl": {"gateOpenSeconds": 14700, "guidanceStartSeconds": 14700,
                                    "guidanceDelaySeconds": 1500, "compliance": 0.4},
                "controlOrigin": "工程测试设定，不是原文数值观测；源文字只记录尝试开放和分流生效的阶段。",
                "reconstruction": "区间进入均匀分布，离开按FIFO重建停留时间，SUMO生成运动轨迹；原始人数只作校准目标。",
            },
            "quality": {"populationResiduals": residuals, "missingMetrics": ["density", "meanSpeed", "pressureProxy"],
                        "flowSemantics": "Interval totals, never persons/second; each entrance is a separate observation region.",
                        "demographicSemantics": "Age and nationality are independent marginal distributions; no joint distribution is inferred."},
        },
    }


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=root.parent / "CrowdSim/frontend/src/views/crowdSim/postanalysis/Simulation_data_ShanghaiMemorialTower_detail.md")
    parser.add_argument("--output", type=Path, default=root / "docs/examples/shanghai_memorial_post_dataset.json")
    args = parser.parse_args()
    result = normalize(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"path": str(args.output), "records": len(result["observations"]),
                      "sourceResidualCount": len(result["metadata"]["quality"]["populationResiduals"]),
                      "durationSeconds": result["metadata"]["durationSeconds"]}))


if __name__ == "__main__":
    main()
