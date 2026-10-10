"""Append observation evidence and atomically publish machine-readable run status."""

from collections import Counter
import csv
import json


class ObservationRecorder:
    def __init__(self, directory, collector):
        self.directory = directory
        self.collector = collector
        self.recorded_samples = 0
        self.valid_samples = Counter()
        self.last_snapshot_id = None
        self.last_sample_time = None
        self.finalized = False
        self.density_differences_written = False
        self.committed_snapshot_ids = set()
        self._atomic_json("observation_geometry.json", collector.geometry_metadata())
        self._write_evacuation_state("running", None)
        self._write_result("running", None)

    def record(self, sample):
        if sample["snapshot_id"] == self.last_snapshot_id:
            return
        if self.finalized:
            raise RuntimeError("observation recording is already finalized")
        self._append_csv("observation_global.csv", [sample["global"]])
        self._append_csv("observation_cells.csv", sample["cells"])
        self._append_csv("observation_boundaries.csv", sample["boundaries"])
        self._append_jsonl("state_transitions.jsonl", sample["transitions"])
        if "evacuation" in sample:
            self._append_jsonl("evacuation_progress.jsonl", [{
                "snapshot_id": sample["snapshot_id"], **sample["evacuation"],
            }])
        # Readers can exclude rows from an interrupted multi-file append using
        # this last-written commit marker, rather than assuming every row is complete.
        self._append_jsonl("observation_samples.jsonl", [{
            "snapshot_id": sample["snapshot_id"], "time_seconds": sample["time_seconds"],
            "cell_rows": len(sample["cells"]), "boundary_rows": len(sample["boundaries"]),
            "transition_rows": len(sample["transitions"]),
        }])
        self.last_snapshot_id = sample["snapshot_id"]
        self.committed_snapshot_ids.add(sample["snapshot_id"])
        self.last_sample_time = sample["time_seconds"]
        self.recorded_samples += 1
        self.valid_samples.update(sample["valid_metric_ids"])
        tracker = self.collector.evacuation
        if tracker is not None and tracker.final is not None and not self.density_differences_written:
            self._write_density_differences()
            self._write_evacuation_state("running", None)
            self._write_result("running", None)
        elif self.recorded_samples % 20 == 0:
            self._write_evacuation_state("running", None)
            self._write_result("running", None)

    def sync_lifecycle(self):
        # Start/application may happen while paused, without a new SUMO tick.
        # Persist their exact baselines immediately, without duplicate CSV rows.
        if self.finalized:
            return
        self._write_evacuation_state("running", None)
        self._write_result("running", None)

    def finalize(self, status, reason, error=None):
        if self.finalized:
            return
        self._write_density_differences()
        self._write_evacuation_state(status, reason)
        self._write_result(status, reason, error)
        self.finalized = True

    def _write_evacuation_state(self, status, reason):
        if self.collector.evacuation is not None:
            evidence = self.collector.evacuation.evidence(status, reason)
            self._guard_completion_evidence(evidence)
            self._atomic_json("evacuation_state.json", evidence)

    def _guard_completion_evidence(self, summary):
        tracker = self.collector.evacuation
        committed = bool(tracker.final and tracker.final["snapshot_id"] in self.committed_snapshot_ids)
        summary["completion_evidence_committed"] = committed
        if tracker.final is not None and not committed:
            summary.update(status="uncommitted_completion", completion_time_seconds=None, total_event_time_seconds=None)
            for metric in summary["metrics"].values():
                if metric["status"] == "complete":
                    metric.update(status="uncommitted_completion", value=None)

    def _write_density_differences(self):
        tracker = self.collector.evacuation
        if self.density_differences_written or tracker is None or tracker.final is None:
            return
        if "absolute-evacuation-density" not in tracker.metric_ids:
            self.density_differences_written = True
            return
        # Final-vs-runtime differences require the final distribution. Derive
        # them once from committed cell rows, streaming rather than keeping a
        # run's full density history in memory. Partial runs have no final file.
        index = self.directory / "observation_samples.jsonl"
        committed = {json.loads(line)["snapshot_id"] for line in index.read_text(encoding="utf-8").splitlines()}
        if tracker.final["snapshot_id"] not in committed:
            return
        path = self.directory / "evacuation_density_differences.csv"
        temporary = path.with_suffix(".csv.tmp")
        fields = ["snapshot_id", "time_seconds", "cell_id", "area_m2",
                  "initial_density_person_per_m2", "runtime_density_person_per_m2", "final_density_person_per_m2",
                  "absolute_initial_runtime_person_per_m2", "absolute_runtime_final_person_per_m2",
                  "absolute_initial_final_person_per_m2"]
        with (self.directory / "observation_cells.csv").open(newline="", encoding="utf-8") as source, temporary.open("w", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            for row in csv.DictReader(source):
                time = float(row["time_seconds"])
                if row["snapshot_id"] not in committed or not tracker.initial["time_seconds"] <= time <= tracker.final["time_seconds"]:
                    continue
                cell = row["cell_id"]
                initial = tracker.initial["cells"][cell]["density_person_per_m2"]
                final = tracker.final["cells"][cell]["density_person_per_m2"]
                current = float(row["density_person_per_m2"]) if row["density_valid"] == "True" else None
                difference = lambda a, b: abs(a - b) if a is not None and b is not None else None
                writer.writerow(dict(zip(fields, [row["snapshot_id"], time, cell, float(row["area_m2"]),
                    initial, current, final, difference(initial, current), difference(current, final), difference(initial, final)])))
        temporary.replace(path)
        self.density_differences_written = True

    def _write_result(self, status, reason, error=None):
        result = self.collector.result(status=status, reason=reason,
            recorded_samples=self.recorded_samples, valid_recorded_samples=self.valid_samples)
        result.update(run_id=self.directory.name, last_snapshot_id=self.last_snapshot_id,
                      last_sample_time_seconds=self.last_sample_time,
                      error=error,
                      complete_snapshot_index="observation_samples.jsonl")
        if "evacuation" in result:
            self._guard_completion_evidence(result["evacuation"])
            for name, metric in result["evacuation"]["metrics"].items():
                result["metrics"][name].update(metric)
            if "absolute-evacuation-density" in result["metrics"]:
                result["metrics"]["absolute-evacuation-density"].update(
                    baseline_file="evacuation_state.json", runtime_density_file="observation_cells.csv",
                    final_comparisons_file="evacuation_density_differences.csv" if self.density_differences_written else None,
                )
        self._atomic_json("observation_result.json", result)

    def _atomic_json(self, name, value):
        path = self.directory / name
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _append_csv(self, name, rows):
        if not rows:
            return
        path = self.directory / name
        exists = path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            if not exists:
                writer.writeheader()
            writer.writerows(rows)

    def _append_jsonl(self, name, records):
        path = self.directory / name
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
