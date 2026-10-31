"""Validate full-source SUMO execution locally, without replacing Mongo storage.

The production app never falls back to this repository. This script is an
explicit engineering validation when the database tunnel is unavailable.
"""
from __future__ import annotations

import hashlib
import argparse
import json
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data_service.config import load_database_env
from postanalysis_api.repositories.local import LocalRepository
from postanalysis_api.services.execution_template import execution_template
from postanalysis_api.workers.analysis import summarize
from postanalysis_api.workers.sumo_batch import SumoBatchExecutor


class ValidationRepository(LocalRepository):
    def begin_archive(self, run_id, kind, user, workspace, configuration):
        folder = self.root / "archives" / run_id
        folder.mkdir(parents=True, exist_ok=True)
        archive = {"runId": run_id, "storage": "local-validation-only", "experimentType": kind, "directory": str(folder)}
        (folder / "configuration.json").write_text(json.dumps(configuration, ensure_ascii=False, indent=2), encoding="utf-8")
        return archive

    def append_trajectory(self, archive, sequence, frames):
        path = Path(archive["directory"]) / f"trajectory-{sequence:05d}.json"
        path.write_text(json.dumps({"sequence": sequence, "frames": frames}, ensure_ascii=False), encoding="utf-8")

    def finish_archive(self, archive, status, result):
        path = Path(archive["directory"]) / "result.json"
        path.write_text(json.dumps({"status": status, "result": result}, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=int, default=19800)
    parser.add_argument("--baseline-only", action="store_true")
    args = parser.parse_args()
    if not 2 <= args.duration <= 19800:
        parser.error("duration must be between 2 and 19800")
    load_database_env()
    base = ROOT / "runs/postanalysis/offline-validation"
    repo = ValidationRepository(base / "records")
    runner = SumoBatchExecutor(repo, base / "sumo")
    data = json.loads((ROOT / "docs/examples/shanghai_memorial_post_dataset.json").read_text(encoding="utf-8"))
    data["id"] = "source-dataset"
    data["version"] = hashlib.sha256(json.dumps({"observations": data["observations"], "metadata": data["metadata"]},
                                               sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    request = execution_template(data)
    for group in request["groups"]:
        group["repeatCount"] = 1
    request["scenario"]["durationSeconds"] = args.duration
    if args.baseline_only:
        request["groups"] = []
    task = {**request, "id": "memorial-full-window", "ownerId": "crowdsim-local-user", "workspaceId": "post-offline-validation", "datasetVersion": data["version"]}
    runs = []
    try:
        matrix = [("F-00", "baseline", [])] + [(group["id"], group["experimentType"], group["interventions"]) for group in request["groups"]]
        for group_id, kind, actions in matrix:
            print(json.dumps({"stage": "running", "group": group_id, "durationSeconds": args.duration}), flush=True)
            run = runner._one(task, data, group_id, kind, 20261023, actions, threading.Event())
            runs.append(run)
            print(json.dumps({"stage": "completed", "group": group_id, "metrics": run["metrics"]}), flush=True)
        report = {"status": "completed", "engineeringOnly": True, "storage": "local-validation-only",
                  "sourceRecords": len(data["observations"]), "durationSeconds": args.duration,
                  "runs": [{key: run.get(key) for key in ("id", "planId", "seed", "metrics", "calibration", "measurement", "outputDirectory")} for run in runs],
                  "analysis": summarize(task, runs)}
        (base / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        print(json.dumps({"status": "completed", "report": str(base / "report.json"), "runCount": len(runs)}), flush=True)
    finally:
        runner.close()


if __name__ == "__main__":
    main()
