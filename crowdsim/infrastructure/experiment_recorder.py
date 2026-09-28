"""Append-only experiment evidence for replay and audit."""

from __future__ import annotations

import csv
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import shutil
from typing import Any

from crowdsim.infrastructure.decision_table_writer import (
    DecisionTableWriter, FORMAT, FORMAT_VERSION, MANIFEST_FILE, REPLAY_SUPPORTED, json_default as _json_default,
)


class ExperimentRecorder:
    def __init__(self, run_id: str, config_path: Path, engine: dict, root: Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[2]
        self.directory = (root or project_root / "runs") / run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        if (self.directory / "manifest.json").exists():
            raise FileExistsError("experiment already exists; start a new run")
        self._seen_profiles = set()
        self._trajectory_path = self.directory / "trajectory.csv"
        self._metrics_path = self.directory / "metrics.csv"
        config_bytes = config_path.read_bytes()
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project_root, capture_output=True, text=True, check=False).stdout.strip()
        except OSError:
            commit = "unknown"
        self.decision_writer = DecisionTableWriter(self.directory)
        try:
            self._write_json(self.directory / "manifest.json", {
                "run_id": run_id, "code_commit": commit, "config_path": str(config_path),
                "config_sha256": hashlib.sha256(config_bytes).hexdigest(), "engine": engine,
                "decision_log_format": FORMAT, "decision_log_version": FORMAT_VERSION,
                "decision_log_manifest": MANIFEST_FILE, "decision_log_replay_supported": REPLAY_SUPPORTED,
            })
            for name in ("commands.jsonl", "messages.jsonl", "lifecycle.jsonl", "profiles.jsonl"):
                (self.directory / name).touch(exist_ok=True)
        except Exception as exc:
            try:
                self.abort(f"recorder initialization failed: {exc}")
            except Exception:
                pass
            raise

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
                writer.writerow(["time", "person_id", "x", "y", "speed", "edge",
                    "density_person_per_m2", "visual_state", "visual_reason", "activity_state",
                    "low_speed_duration_seconds", "blocked_duration_seconds",
                    "critical_density_duration_seconds", "dense_low_speed_duration_seconds",
                    "visual_blocked_duration_seconds"])
            for motion in result.persons.values():
                visual = runtime.visual_states[motion.person_id]
                state = runtime.population.states[motion.person_id]
                writer.writerow([result.time_seconds, motion.person_id, motion.x, motion.y, motion.speed, motion.edge_id,
                    visual["density_person_per_m2"], visual["visual_state"], visual["visual_reason"], state.activity_state,
                    visual["low_speed_duration_seconds"], visual["blocked_duration_seconds"],
                    visual["critical_density_duration_seconds"], visual["dense_low_speed_duration_seconds"],
                    visual["visual_blocked_duration_seconds"]])
        metrics_row = {"time": result.time_seconds, "pedestrian_count": metrics["pedestrian_count"], "vehicle_count": metrics["vehicle_count"], "pedestrian_avg_speed_mps": metrics["pedestrian_avg_speed_mps"], "vehicle_avg_speed_mps": metrics["vehicle_avg_speed_mps"], "conservation_error": metrics["population"]["conservation_error"]}
        metrics_row.update({
            f"visual_{name}_count": count
            for name, count in metrics.get("visual_state_counts", {}).items()
        })
        metrics_row["visual_density_unknown_count"] = metrics.get("visual_density_unknown_count", 0)
        exists = self._metrics_path.exists()
        with self._metrics_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(metrics_row))
            if not exists:
                writer.writeheader()
            writer.writerow(metrics_row)

    def record_decision(self, plan, result, *, context=None, candidates=()) -> None:
        self.decision_writer.write_record({"context": context, "candidates": list(candidates), "plan": plan, "execution": result})

    def record_command(self, command: dict, result: dict) -> None:
        self._append_jsonl("commands.jsonl", [{"command": command, "result": result}])

    def record_messages(self, records) -> None:
        self._append_jsonl("messages.jsonl", list(records))

    def finalize(self, runtime) -> None:
        self.close_logs(runtime)
        self.write_summary(runtime)

    def close_logs(self, runtime) -> None:
        writer = self.decision_writer
        writer.close(aborted=bool(runtime.last_error), reason=runtime.last_error)
        if writer.failed:
            raise OSError(writer.error or "decision log finalization failed")

    def write_summary(self, runtime) -> None:
        # Publish only a fully written summary; a failed write must not leave
        # a truncated JSON file in place of the previous diagnostic evidence.
        temporary = self.directory / "summary.json.tmp"
        self._write_json(temporary, runtime.diagnostics())
        temporary.replace(self.directory / "summary.json")

    def abort(self, reason: str) -> None:
        self.decision_writer.close(aborted=True, reason=reason)

    @property
    def decision_log_diagnostics(self) -> dict:
        writer = self.decision_writer
        return {
            "format": FORMAT, "format_version": FORMAT_VERSION, "status": writer.status,
            "closed": writer.closed, "replay_supported": REPLAY_SUPPORTED,
            "records": writer.rows["decisions"], "total_bytes": sum(writer.bytes.values()),
            "table_rows": dict(writer.rows), "flushes": writer.flushes,
            "error": writer.error,
            "cache_evictions": {name: cache.evictions for name, cache in writer.caches.items()},
        }

    def _append_jsonl(self, name: str, records: list[Any]) -> None:
        if not records:
            return
        with (self.directory / name).open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n", encoding="utf-8")
