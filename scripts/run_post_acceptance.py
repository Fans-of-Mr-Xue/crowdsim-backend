"""Run real SUMO -> Mongo -> analysis checks in an isolated workspace.

Smoke uses a small synthetic fixture; memorial uses the complete source window
with the engineering test model. Records are retained in Mongo and the report
contains their IDs. Neither profile establishes historical causal conclusions.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data_service.config import load_database_env
from postanalysis_api.services.application import PostAnalysisApp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=600)
    parser.add_argument("--profile", choices=("smoke", "memorial"), default="smoke")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=None,
                        help="Overall deadline; full-window checks default to 15 minutes per planned run")
    args = parser.parse_args()
    if args.timeout_seconds is not None and args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    load_database_env()
    user, workspace = "crowdsim-local-user", "post-acceptance-" + uuid4().hex[:12]
    app = PostAnalysisApp(ROOT / "runs/postanalysis", reconcile=False)
    try:
        capability = app.capabilities()["simulation"]
        if not capability["ready"]:
            raise RuntimeError("SUMO not ready")
        # Small independent fixture: explicit arrivals and releases exercise routing.
        observations, records = [], []
        for region, count in (("A", 12), ("B", 6), ("C", 0)):
            for seconds, entered, exited, population in ((0, count, 0, count), (300, 0, count, 0), (600, 0, 0, 0)):
                observations.append({"timeSeconds": seconds, "regionId": region, "population": population,
                                     "density": None, "meanSpeed": None, "pressureProxy": None})
                records.append({"timeSeconds": seconds, "regionId": region, "intervalEntered": entered, "intervalExited": exited})
        dataset = app.create_dataset(user, workspace, {"name": "工程验收小样本（非完整纪念塔实验）", "sourceKind": "synthetic_reference",
            "sourceNote": "18人、600秒的工程链路验收；非原始纪念塔数据，结果不能代替正式实验。",
            "observations": observations, "metadata": {"durationSeconds": 600, "sourceRecords": records}}) if args.profile == "smoke" else {"id": ""}
        scenario = {"scenarioId": capability["scenarioIds"][0], "modelId": capability["modelIds"][0],
                    "modelVersion": capability["modelVersions"][capability["modelIds"][0]][0], "startClock": "20:00",
                    "durationSeconds": args.duration, "geometryVersion": next(iter(capability["geometryVersions"].values()))[0],
                    "ruleVersion": capability["ruleVersions"][0]}
        groups = []
        for index, (kind, compliance, reference) in enumerate((("observation", 1, "F-00"), ("intervention", 0.5, "F-00"), ("mechanism", 0, "CF-01")), 1):
            groups.append({"id": f"CF-{index:02d}", "name": kind + "验收", "repeatCount": 2,
                "hypothesis": "验证真实路线编译和配对分析入库", "experimentType": kind, "referenceGroupId": reference,
                "manipulatedFeatureId": "early_density", "interventions": [
                    {"id": "open", "targetRegionId": "C", "actionType": "open_gate", "atSeconds": 30, "parameters": {}},
                    {"id": "guide", "targetRegionId": "C", "actionType": "guidance", "atSeconds": 30,
                     "parameters": {"compliance": compliance, "delaySeconds": 0}}]})
        body = {"name": "事后工程链路验收（小样本）", "datasetId": dataset["id"], "scenario": scenario,
                "seedStart": 20261023, "parallelism": 1, "groups": groups, "metricIds": capability["metricIds"],
                "pcFeatures": [{"id": "early_density", "metricId": "peak_density", "regionId": "global",
                                "windowStartSeconds": 0, "windowEndSeconds": args.duration // 2, "aggregation": "peak"},
                               {"id": "late_speed", "metricId": "mean_speed", "regionId": "global",
                                "windowStartSeconds": args.duration // 2, "windowEndSeconds": args.duration, "aggregation": "mean"}]}
        if args.profile == "memorial":
            from postanalysis_api.services.execution_template import execution_template
            source_path = ROOT / "docs/examples/shanghai_memorial_post_dataset.json"
            payload = json.loads(source_path.read_text(encoding="utf-8"))
            payload["rawFile"] = {"name": source_path.name, "contentType": "application/json",
                                  "data": base64.b64encode(source_path.read_bytes()).decode("ascii")}
            dataset = app.create_dataset(user, workspace, payload)
            body = execution_template(dataset)
            for group in body["groups"]:
                group["repeatCount"] = args.repeats
            body["name"] = f"纪念塔完整时窗工程验收（每组{args.repeats}次）"
        task = app.create_experiment(user, workspace, body)
        app.start("experiments", task["id"], user, workspace)
        planned_runs = max(group["repeatCount"] for group in body["groups"]) + sum(group["repeatCount"] for group in body["groups"])
        timeout_seconds = args.timeout_seconds or (900 * planned_runs if args.profile == "memorial" else 180)
        deadline = time.monotonic() + timeout_seconds
        last_progress = None
        while True:
            task = app.repo.get("experiments", task["id"], user, workspace)
            progress = task["progress"].get("baselineCompleted", 0) + task["progress"].get("interventionCompleted", 0)
            if progress != last_progress:
                print(json.dumps({"profile": args.profile, "status": task["status"], "completedRuns": progress}), flush=True)
                last_progress = progress
            if task["status"] in {"completed", "failed", "cancelled"}:
                break
            if time.monotonic() > deadline:
                app.cancel("experiments", task["id"], user, workspace)
                raise RuntimeError("Acceptance deadline exceeded")
            time.sleep(0.5)
        if task["status"] != "completed":
            raise RuntimeError(task.get("error", "Acceptance did not complete"))
        sections = {section: app.result(task["id"], user, workspace, section)["data"]
                    for section in ("observation", "intervention", "mechanism", "causalGraph")}
        runs = [run for run in app.repo.list("runs", user, workspace, limit=1000) if run["experimentId"] == task["id"]]
        task["archive"] = app.repo.archive_entry(task["id"], user, workspace)
        for run in runs:
            run["archive"] = app.repo.archive_entry(run["id"], user, workspace)
        trajectory = app.repo.trajectory(runs[0]["id"], user, workspace, -1, 1)
        if not trajectory["items"] or not any(frame["persons"] for frame in trajectory["items"][0]["frames"]):
            raise RuntimeError("No real SUMO trajectory stored")
        report = {"status": "completed", "engineeringOnly": True, "user": user, "workspace": workspace,
                  "experimentId": task["id"], "datasetId": dataset["id"], "resultId": task["resultId"],
                  "runCount": len(runs), "archive": task.get("archive"), "analysis": sections,
                  "runs": [{key: run.get(key) for key in ("id", "planId", "seed", "metrics", "archive")} for run in runs]}
        output = ROOT / f"runs/postanalysis/acceptance-{args.profile}.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        print(json.dumps({"status": report["status"], "runCount": len(runs), "report": str(output)}, ensure_ascii=True))
    finally:
        app.close()


if __name__ == "__main__":
    main()
