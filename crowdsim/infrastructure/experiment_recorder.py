"""Append-only experiment evidence for replay and audit."""

from __future__ import annotations

import csv
from dataclasses import asdict, is_dataclass
import hashlib
import json
from pathlib import Path
import subprocess
import shutil
from typing import Any


def _json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


class ExperimentRecorder:
    def __init__(self, run_id: str, config_path: Path, engine: dict, root: Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[2]
        self.directory = (root or project_root / "runs") / run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self._seen_profiles = set()
        self._trajectory_path = self.directory / "trajectory.csv"
        self._metrics_path = self.directory / "metrics.csv"
        config_bytes = config_path.read_bytes()
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project_root, capture_output=True, text=True, check=False).stdout.strip()
        except OSError:
            commit = "unknown"
        self._write_json(self.directory / "manifest.json", {"run_id": run_id, "code_commit": commit, "config_path": str(config_path), "config_sha256": hashlib.sha256(config_bytes).hexdigest(), "engine": engine})
        for name in ("commands.jsonl", "messages.jsonl", "decisions.jsonl", "lifecycle.jsonl", "profiles.jsonl"):
            (self.directory / name).touch(exist_ok=True)

    def archive_demand(self, source: Path) -> None:
        destination = self.directory / "demand.rou.xml"
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)
        self.update_manifest(demand_sha256=hashlib.sha256(destination.read_bytes()).hexdigest())

    def update_manifest(self, **fields: Any) -> None:
        path = self.directory / "manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(fields)
        self._write_json(path, payload)

    def record_step(self, runtime, result, metrics: dict) -> None:
        self._append_jsonl("lifecycle.jsonl", [{"time": result.time_seconds, "event": "departed", "person_id": person_id} for person_id in result.departed_person_ids] + [{"time": result.time_seconds, "event": "arrived", "person_id": person_id} for person_id in result.arrived_person_ids])
        new_profiles = []
        for person_id in result.persons:
            if person_id not in self._seen_profiles:
                new_profiles.append(asdict(runtime.population.profile_for(person_id)))
                self._seen_profiles.add(person_id)
        self._append_jsonl("profiles.jsonl", new_profiles)
        exists = self._trajectory_path.exists()
        with self._trajectory_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if not exists:
                writer.writerow(["time", "person_id", "x", "y", "speed", "edge"])
            for motion in result.persons.values():
                writer.writerow([result.time_seconds, motion.person_id, motion.x, motion.y, motion.speed, motion.edge_id])
        metrics_row = {"time": result.time_seconds, "pedestrian_count": metrics["pedestrian_count"], "vehicle_count": metrics["vehicle_count"], "pedestrian_avg_speed_mps": metrics["pedestrian_avg_speed_mps"], "vehicle_avg_speed_mps": metrics["vehicle_avg_speed_mps"], "conservation_error": metrics["population"]["conservation_error"]}
        exists = self._metrics_path.exists()
        with self._metrics_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(metrics_row))
            if not exists:
                writer.writeheader()
            writer.writerow(metrics_row)

    def record_decision(self, plan, result, *, context=None, candidates=()) -> None:
        self._append_jsonl("decisions.jsonl", [{"context": context, "candidates": list(candidates), "plan": plan, "execution": result}])

    def record_command(self, command: dict, result: dict) -> None:
        self._append_jsonl("commands.jsonl", [{"command": command, "result": result}])

    def record_messages(self, records) -> None:
        self._append_jsonl("messages.jsonl", list(records))

    def finalize(self, runtime) -> None:
        self._write_json(self.directory / "summary.json", runtime.diagnostics())

    def _append_jsonl(self, name: str, records: list[Any]) -> None:
        if not records:
            return
        with (self.directory / name).open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n", encoding="utf-8")
