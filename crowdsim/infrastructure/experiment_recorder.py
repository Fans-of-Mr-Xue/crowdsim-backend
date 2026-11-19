"""Append-only experiment evidence for replay and audit."""

from __future__ import annotations

import csv
from contextlib import nullcontext
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import subprocess
import shutil
from typing import Any

from crowdsim.infrastructure.decision_table_writer import (
    DecisionTableWriter, FORMAT, FORMAT_VERSION, MANIFEST_FILE, REPLAY_SUPPORTED, json_default as _json_default,
)
from crowdsim.infrastructure.observation_recorder import ObservationRecorder
from crowdsim.infrastructure.performance_probe import timed


class ExperimentRecorder:
    def __init__(self, run_id: str, config_path: Path, engine: dict, root: Path | None = None) -> None:
        project_root = Path(__file__).resolve().parents[2]
        self.run_id = run_id
        self.directory = (root or project_root / "runs") / run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        if (self.directory / "manifest.json").exists():
            raise FileExistsError("experiment already exists; start a new run")
        self._seen_profiles = set()
        self._post_action_registered = False
        self._runtime_observation_registered = False
        self._runtime_observation_last = None
        self._trace_registered = False
        self.observation_writer = None
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
        if getattr(runtime, 'learning_controller', None) is not None:
            self.record_runtime_observations(runtime, result)
        observation = metrics.get("observation")
        extra_fields = ["snapshot_id", "in_observation_scope", "observation_cell_id",
            "behavior_state", "behavior_reason", "crowded", "psychological_state",
            "stress", "fatigue", "perceived_risk", "perceived_crowding"] if observation else []
        self._append_jsonl("lifecycle.jsonl", [{"time": result.time_seconds, "event": "departed", "person_id": person_id} for person_id in result.departed_person_ids] + [{"time": result.time_seconds, "event": "arrived", "person_id": person_id} for person_id in result.arrived_person_ids])
        new_profiles = []
        for person_id in result.persons:
            if person_id not in self._seen_profiles:
                new_profiles.append(asdict(runtime.population.profile_for(person_id)))
                self._seen_profiles.add(person_id)
        self._append_jsonl("profiles.jsonl", new_profiles)
        exists = self._trajectory_path.exists()
        with self._measure('trajectory_log_write'), self._trajectory_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if not exists:
                writer.writerow(["time", "person_id", "x", "y", "speed", "edge",
                    "density_person_per_m2", "visual_state", "visual_reason", "activity_state",
                    "low_speed_duration_seconds", "blocked_duration_seconds",
                    "critical_density_duration_seconds", "dense_low_speed_duration_seconds",
                    "visual_blocked_duration_seconds", *extra_fields])
            for motion in result.persons.values():
                visual = runtime.visual_states[motion.person_id]
                state = runtime.population.states[motion.person_id]
                row = [result.time_seconds, motion.person_id, motion.x, motion.y, motion.speed, motion.edge_id,
                    visual["density_person_per_m2"], visual["visual_state"], visual["visual_reason"], state.activity_state,
                    visual["low_speed_duration_seconds"], visual["blocked_duration_seconds"],
                    visual["critical_density_duration_seconds"], visual["dense_low_speed_duration_seconds"],
                    visual["visual_blocked_duration_seconds"]]
                if observation:
                    agent = observation["agents"].get(motion.person_id, {})
                    row.extend([observation["snapshot_id"], agent.get("in_scope"), agent.get("cell_id"),
                                *[agent.get(field) for field in extra_fields[3:]]])
                writer.writerow(row)
        metrics_row = {"time": result.time_seconds, "pedestrian_count": metrics["pedestrian_count"], "vehicle_count": metrics["vehicle_count"], "pedestrian_avg_speed_mps": metrics["pedestrian_avg_speed_mps"], "vehicle_avg_speed_mps": metrics["vehicle_avg_speed_mps"], "conservation_error": metrics["population"]["conservation_error"]}
        metrics_row.update({
            f"visual_{name}_count": count
            for name, count in metrics.get("visual_state_counts", {}).items()
        })
        metrics_row["visual_density_unknown_count"] = metrics.get("visual_density_unknown_count", 0)
        exists = self._metrics_path.exists()
        with self._measure('metrics_log_write'), self._metrics_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(metrics_row))
            if not exists:
                writer.writeheader()
            writer.writerow(metrics_row)
        if self.observation_writer is not None and observation is not None:
            self.observation_writer.record(observation)
        during = metrics.get("during")
        if during is not None:
            self._append_jsonl("during_observations.jsonl", [during])
            if during["finished"]:
                self._write_json(self.directory / "during_result.json", during)

    def attach_during_preset(self, preset) -> None:
        from crowdsim.domain.during_preset import preset_fingerprint
        self._write_json(self.directory / "during_preset.json", preset)
        self.update_manifest(during={
            "preset_id": preset["preset_id"], "preset_version": preset["version"],
            "preset_fingerprint": preset_fingerprint(preset), "preset_file": "during_preset.json",
            "observations_file": "during_observations.jsonl", "result_file": "during_result.json",
        })

    def attach_observations(self, collector) -> None:
        if collector is not None:
            self.observation_writer = ObservationRecorder(self.directory, collector)
            self.observation_writer.performance = getattr(self, 'performance', None)
            self.update_manifest(observation={
                **collector.config.metadata(), "metric_ids": list(collector.metric_ids),
                "requested_metric_ids": list(collector.requested_ids),
                "computed_metric_ids": sorted(collector.computed_ids),
                "network_sha256": collector.network_sha256,
                "geometry_file": "observation_geometry.json",
                "result_file": "observation_result.json",
                "evacuation_state_file": "evacuation_state.json" if collector.evacuation else None,
                "evacuation_progress_file": "evacuation_progress.jsonl" if collector.evacuation else None,
                "density_differences_file": "evacuation_density_differences.csv" if "absolute-evacuation-density" in collector.metric_ids else None,
            })

    def record_observation_lifecycle(self) -> None:
        if self.observation_writer is not None:
            self.observation_writer.sync_lifecycle()

    def finalize_observations(self, status: str, reason: str, error=None) -> None:
        if self.observation_writer is not None:
            self.observation_writer.finalize(status, reason, error)

    def record_decision(self, plan, result, *, context=None, candidates=()) -> None:
        self.decision_writer.write_record({"context": context, "candidates": list(candidates), "plan": plan, "execution": result})

    def record_decision_trace(self, trace: dict) -> None:
        """Append the additive per-person decision evidence chain (read-only projection).

        The trace is a separate append-only file; existing decision tables and
        their manifest layout are unchanged.
        """
        if not isinstance(trace, dict) or trace.get("version") != "decision-trace-v1":
            raise ValueError("invalid decision trace record")
        path = self.directory / "decision_traces.jsonl"
        with path.open("a+", encoding="utf-8") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            try:
                handle.write(json.dumps(trace, ensure_ascii=False, sort_keys=True,
                                         default=_json_default) + "\n")
                handle.flush()
            except Exception:
                handle.seek(position)
                handle.truncate()
                raise
        if not self._trace_registered:
            self.update_manifest(decision_trace_log={"version": "decision-trace-v1",
                                                     "file": "decision_traces.jsonl"})
            self._trace_registered = True

    def record_post_action(self, transitions) -> None:
        # Roll back only this append if persistence fails; the journal keeps its
        # unacknowledged batch for an explicit retry. Never publish partial JSON.
        lines = ''.join(item.payload_json + '\n' for item in transitions)
        if not lines:
            return
        if not self._post_action_registered:
            self.update_manifest(post_action={'version':'post-action-evidence-v1',
                'file':'post_action.jsonl','interval_semantics':'next_physical_step',
                'reward_status':'not_evaluated','policy_updates':0})
            self._post_action_registered = True
        path = self.directory / 'post_action.jsonl'
        with path.open('a+', encoding='utf-8') as handle:
            handle.seek(0, 2)
            position = handle.tell()
            try:
                handle.write(lines)
                handle.flush()
            except Exception:
                handle.seek(position)
                handle.truncate()
                raise

    def record_runtime_observations(self, runtime, result=None) -> None:
        """Independent unrounded source observations, never gated by metric selection.

        Only active learners request this stream. No state/metric risk fallback.
        Last-snapshot guard is bounded; replaying old physical time is refused.
        Append failure rolls back this batch and does not advance the guard.
        """
        result = result if result is not None else runtime.current
        now = runtime.time_seconds
        snapshot = runtime.snapshot_id
        if (runtime.run_id != self.run_id or not isinstance(snapshot, str)
                or not snapshot.startswith(runtime.run_id + ':')
                or type(now) not in (int, float) or not math.isfinite(now) or now < 0
                or getattr(result, 'time_seconds', now) != now):
            raise ValueError('invalid runtime observation archive boundary')
        rows = []
        observations = getattr(runtime, 'observations', {})
        for pid, motion in sorted(result.persons.items()):
            raw_motion = asdict(motion)
            if motion.person_id != pid or motion.time_seconds != now:
                raise ValueError('runtime observation archive motion binding mismatch')
            if any(type(raw_motion[k]) not in (int, float) or not math.isfinite(raw_motion[k])
                   for k in ('time_seconds', 'x', 'y', 'lon', 'lat', 'speed', 'lane_position', 'angle')):
                raise ValueError('nonfinite runtime observation archive motion')
            obs = observations.get(pid)
            status = 'observed'
            if obs is None:
                status = 'missing_observation'
            elif (obs.person_id != pid or obs.snapshot_id != snapshot
                  or obs.time_seconds != now or obs.own_motion != motion):
                status = 'observation_binding_mismatch'
            elif (any(type(getattr(obs, k)) not in (int, float)
                      or not math.isfinite(getattr(obs, k)) or not 0 <= getattr(obs, k) <= 1
                      for k in ('perceived_risk', 'perceived_crowding'))
                  or (obs.objective_density_per_m2 is not None
                      and (type(obs.objective_density_per_m2) not in (int, float)
                           or not math.isfinite(obs.objective_density_per_m2)
                           or obs.objective_density_per_m2 < 0))):
                status = 'invalid_observation_values'
            state = runtime.population.states.get(pid)
            state_matches = state is not None and state.person_id == pid
            profile = runtime.population.profile_for(pid)
            if profile.person_id != pid:
                raise ValueError('runtime observation archive profile binding mismatch')
            rows.append(dict(version='runtime-observation-source-v1', run_id=runtime.run_id,
                snapshot_id=snapshot, time_seconds=now, person_id=pid, role_id=profile.crowd_role,
                status=status, source='runtime.observations', motion=raw_motion,
                perceived_risk=obs.perceived_risk if status == 'observed' else None,
                perceived_crowding=obs.perceived_crowding if status == 'observed' else None,
                objective_density=obs.objective_density_per_m2 if status == 'observed' else None,
                state={k: getattr(state, k) if state_matches
                          and type(getattr(state, k)) in (int, float)
                          and math.isfinite(getattr(state, k)) else None
                       for k in ('stress', 'fatigue', 'perceived_risk', 'perceived_crowding')}))
        lines = ''.join(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + '\n'
                        for row in rows)
        digest = hashlib.sha256(lines.encode('utf-8')).hexdigest()
        last = self._runtime_observation_last
        if last is not None:
            if (snapshot, now) == last[:2]:
                if digest != last[2]:
                    raise ValueError('conflicting same-boundary runtime observations')
                return
            if now <= last[1] or snapshot == last[0]:
                raise ValueError('stale runtime observation archive boundary')
        if not self._runtime_observation_registered:
            self.update_manifest(runtime_observations={
                'version': 'runtime-observation-source-v1', 'file': 'runtime_observations.jsonl',
                'source': 'runtime.observations', 'precision': 'unrounded',
                'missing_policy': 'explicit_unavailable_null', 'metric_selection_independent': True})
            self._runtime_observation_registered = True
        path = self.directory / 'runtime_observations.jsonl'
        with path.open('a+', encoding='utf-8') as handle:
            handle.seek(0, 2)
            position = handle.tell()
            try:
                handle.write(lines)
                handle.flush()
            except Exception:
                handle.seek(position)
                handle.truncate()
                raise
        self._runtime_observation_last = (snapshot, now, digest)

    def record_learning_runtime(self, batch) -> None:
        # Frozen JSON audit records are acknowledged only after a full append.
        lines = ''.join(row + '\n' for row in batch)
        if not lines:
            return
        path = self.directory / 'learning_runtime.jsonl'
        with path.open('a+', encoding='utf-8') as handle:
            handle.seek(0, 2)
            position = handle.tell()
            try:
                handle.write(lines)
                handle.flush()
            except Exception:
                handle.seek(position)
                handle.truncate()
                raise

    def record_command(self, command: dict, result: dict) -> None:
        self._append_jsonl("commands.jsonl", [{"command": command, "result": result}])

    def record_messages(self, records) -> None:
        self._append_jsonl("messages.jsonl", list(records))

    def finalize(self, runtime) -> None:
        self.close_logs(runtime)
        self.finalize_observations("error" if runtime.last_error else "interrupted", "runtime_finalized", runtime.last_error)
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

    def _measure(self, name):
        probe = getattr(self, 'performance', None)
        return probe.measure(name) if probe is not None and probe.enabled else nullcontext()

    @timed('business_jsonl_write')
    def _append_jsonl(self, name: str, records: list[Any]) -> None:
        if not records:
            return
        with (self.directory / name).open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n", encoding="utf-8")
