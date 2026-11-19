"""Dedicated post-review SUMO processes, isolated from the during-stage runtime."""
from __future__ import annotations

from copy import deepcopy
from collections import Counter, OrderedDict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import threading
import xml.etree.ElementTree as ET

from crowdsim.infrastructure.sumo_adapter import discover_sumo_binary
from postanalysis_api.schemas.common import ApiError
from postanalysis_api.schemas.metrics import METRIC_VERSION
from .analysis import summarize
from .controlled_sumo import execute
from .batch_state import SupersededExecution, completed_batch_runs, restored_progress, run_matrix
from .measurement import SAMPLE_SECONDS, geometry, measure, region_cells
from .memorial_scenario import (ACTION_SCHEMAS, ENTRY_EDGES, METRIC_IDS, MODEL_ID, MODEL_VERSION,
                                NET_FILE, RULE_VERSION, compile_routes, control_schedule, geometry_version, source_cohorts)


class SumoBatchExecutor:
    def __init__(self, repository, data_dir):
        self.repo, self.data_dir = repository, Path(data_dir)
        self._lock, self._slot = threading.RLock(), threading.Semaphore(1)
        self._jobs, self._threads = {}, {}
        self._pause_requests = {}
        self._runtime = OrderedDict()
        try:
            self.binary, self.unavailable = discover_sumo_binary(), None
        except FileNotFoundError:
            self.binary, self.unavailable = None, "SUMO_BINARY_MISSING"

    def capabilities(self):
        if not self.binary:
            return {"ready": False, "reason": self.unavailable}
        regions = geometry()
        return {"ready": True, "scenarioIds": ["memorial-post-test"], "modelIds": [MODEL_ID],
                "modelVersions": {MODEL_ID: [MODEL_VERSION]},
                "geometryVersions": {"memorial-post-test": [geometry_version()]}, "ruleVersions": [RULE_VERSION],
                "actionTypes": list(ACTION_SCHEMAS), "actionSchemas": deepcopy(ACTION_SCHEMAS),
                "regionMap": {key: {"edgeIds": [edge], "cellIds": [f"g_{x}_{y}" for x, y in sorted(region_cells()[key])],
                                    "effectiveAreaM2": regions[key].area} for key, edge in ENTRY_EDGES.items()},
                "metricIds": METRIC_IDS, "maxParallelism": 1, "maxTotalRuns": 650,
                "samplingSeconds": SAMPLE_SECONDS,
                "factualReconstruction": {"inputMode": "spatial-distribution/2.0", "configurationSource": "verified_during_archive",
                    "samplingSource": "source_observation_clock", "geometrySource": "source_checked_during_observation_cells",
                    "initialization": "source_first_frame_spatial_distribution", "calibrated": False},
                "mappingNote": "A→M1、B→M2、C→M3 为用户确认的工程测试映射；M3 不是真实北侧通道"}

    def submit(self, task, on_event):
        dataset = self.repo.get("datasets", task["datasetId"], task["ownerId"], task["workspaceId"])
        from postanalysis_api.services.population_execution import prepare_execution_dataset
        dataset = prepare_execution_dataset(dataset)
        source_cohorts(dataset)  # Fail synchronously before accepting an invalid job.
        for group in task.get("groups", []):
            control_schedule(group["interventions"], dataset.get('metadata', {}).get('executionProfile', {}).get('baselineControl'))
        if task["scenario"]["durationSeconds"] > dataset.get("metadata", {}).get("durationSeconds", 0):
            raise ApiError("EXECUTION_INPUT_INVALID", "运行窗口不得超出已上传基线时间窗", 422)
        with self._lock:
            if task["id"] in self._jobs:
                raise ApiError("INVALID_TASK_STATE", "任务已经提交", 409)
            cancel = threading.Event()
            self._jobs[task["id"]] = cancel
            self._pause_requests[task['id']] = threading.Event()
            self._publish(task["id"], status="queued", activeRun=None, frame=None)
            worker = threading.Thread(target=self._run, args=(deepcopy(task), dataset, on_event, cancel), daemon=True,
                                      name="post-sumo-" + task["id"])
            self._threads[task["id"]] = worker
            worker.start()

    def runtime(self, task_id):
        """Latest in-memory frame, never an inferred or synthesized position."""
        with self._lock:
            return deepcopy(self._runtime.get(task_id))

    def is_active(self, task_id):
        with self._lock:
            return task_id in self._jobs

    def set_paused(self, task_id, paused, transition):
        """Serialize database control transitions with the worker's pause gate."""
        with self._lock:
            request = self._pause_requests.get(task_id)
            if task_id not in self._jobs or request is None:
                raise ApiError('INVALID_TASK_STATE', '原执行线程已退出，请从任务记录恢复剩余实验。', 409)
            updated = transition()
            if paused:
                request.set()
            else:
                request.clear()
                self._publish(task_id, status=updated['status'])
            return updated

    def _checkpoint(self, task, on_event, cancel):
        if cancel.is_set():
            raise InterruptedError()
        with self._lock:
            request = getattr(self, '_pause_requests', {}).get(task['id'])
            if request is None or not request.is_set():
                return
            on_event({'status': 'paused'})
            self._publish(task['id'], status='paused')
        while request.is_set():
            if cancel.wait(0.1):
                raise InterruptedError()
        if cancel.is_set():
            raise InterruptedError()

    def _publish(self, task_id, **changes):
        with self._lock:
            state = self._runtime.setdefault(task_id, {"sequence": 0, "activeRun": None, "frame": None})
            state.update(deepcopy(changes))
            state.update(sequence=state["sequence"] + 1,
                         updatedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
            self._runtime.move_to_end(task_id)
            # Keep running/queued jobs, and at most 64 terminal display frames.
            terminal = [key for key, item in self._runtime.items()
                        if item.get("status") in {"completed", "cancelled", "failed", "interrupted"}]
            for key in terminal[:-64]:
                self._runtime.pop(key, None)

    def cancel(self, task_id):
        with self._lock:
            cancel = self._jobs.get(task_id)
            if cancel:
                cancel.set()

    def close(self):
        with self._lock:
            for cancel in self._jobs.values():
                cancel.set()
            workers = list(self._threads.values())
        for worker in workers:
            worker.join(timeout=15)
        alive = [worker.name for worker in workers if worker.is_alive()]
        if alive:
            raise RuntimeError('事后执行线程未退出：' + ', '.join(alive))
        with self._lock:
            self._runtime.clear()
            self._jobs.clear()
            self._threads.clear()
            getattr(self, '_pause_requests', {}).clear()

    def _run(self, task, dataset, on_event, cancel):
        user, workspace = task["ownerId"], task["workspaceId"]
        archive = None
        acquired = False
        runs = []
        progress = deepcopy(task["progress"])
        try:
            completed = completed_batch_runs(self.repo, task) if task.get('resumeCount') else {}
            if completed:
                runs = list(completed.values())
                progress = restored_progress(task, completed)
            while not acquired:
                self._checkpoint(task, on_event, cancel)
                if cancel.is_set():
                    raise InterruptedError()
                acquired = self._slot.acquire(timeout=0.5)
            if cancel.is_set():
                raise InterruptedError()
            self._checkpoint(task, on_event, cancel)
            on_event({"status": "running", 'progress': deepcopy(progress)})
            self._publish(task["id"], status="running")
            archive = deepcopy(task.get('archive')) if task.get('resumeCount') else None
            if task.get('resumeCount'):
                try:
                    archive = self.repo.archive_entry(task['id'], user, workspace)
                except ApiError as exc:
                    if exc.code != 'NOT_FOUND':
                        raise
                    archive = None
            if archive:
                self.repo.reopen_archive(archive)
            else:
                archive = self.repo.begin_archive(task["id"], "experiment" if "groups" in task else "simulation", user, workspace, task)
            self.repo.update("experiments" if "groups" in task else "simulations", task["id"], user, workspace, {"archive": archive})
            groups = task.get("groups", [])
            matrix = run_matrix(task)
            for run_index, (plan_id, kind, seed, actions) in enumerate(matrix, start=1):
                self._checkpoint(task, on_event, cancel)
                if cancel.is_set():
                    raise InterruptedError()
                if (plan_id, seed) in completed:
                    continue
                run = self._one(task, dataset, plan_id, kind, seed, actions, cancel,
                                on_event=on_event, run_index=run_index, total_runs=len(matrix))
                runs.append(run)
                if groups:
                    if plan_id == "F-00":
                        progress["baselineCompleted"] += 1
                    else:
                        progress["interventionCompleted"] += 1
                        next(row for row in progress["groups"] if row["groupId"] == plan_id)["completed"] += 1
                else:
                    progress["completed"] = 1
                on_event({"status": "running", "progress": deepcopy(progress), "activePlanId": plan_id, "runId": run["id"]})
            self._checkpoint(task, on_event, cancel)
            result = summarize(task, runs)
            if cancel.is_set():
                raise InterruptedError()
            result.update(datasetId=task["datasetId"], datasetVersion=task["datasetVersion"],
                          experimentId=task["id"], archive=archive)
            document = self.repo.create("results", user, workspace, result)
            self.repo.finish_archive(archive, "completed", result)
            on_event({"status": "completed", "progress": progress, "resultId": document["id"]})
            self._publish(task["id"], status="completed")
        except SupersededExecution:
            # A newer attempt owns the batch/archive; stop without overwriting it.
            self._publish(task['id'], status='interrupted')
        except InterruptedError:
            if archive:
                self.repo.finish_archive(archive, "cancelled", {"completedRuns": len(runs)})
            self._publish(task["id"], status="cancelled")
            on_event({"status": "cancelled", "activeRun": self.runtime(task["id"]).get("activeRun")})
        except Exception as exc:
            logging.getLogger("postanalysis_api.sumo_batch").exception("Post SUMO batch failed: %s", task["id"])
            try:
                if archive:
                    self.repo.finish_archive(archive, "failed", {"error": str(exc)[:2000], "completedRuns": len(runs)})
                self._publish(task["id"], status="failed")
                active = self.runtime(task["id"]).get("activeRun")
                if active and active.get("phase") == "failed":
                    progress["failed"] = progress.get("failed", 0) + 1
                    for row in progress.get("groups", []):
                        if row["groupId"] == active["planId"]:
                            row["failed"] += 1
                on_event({"status": "failed", "error": str(exc)[:2000], "progress": progress, "activeRun": active})
            except Exception:
                logging.getLogger("postanalysis_api.sumo_batch").exception("Could not persist terminal state")
        finally:
            if acquired:
                self._slot.release()
            with self._lock:
                self._jobs.pop(task["id"], None)
                self._threads.pop(task["id"], None)
                getattr(self, '_pause_requests', {}).pop(task['id'], None)

    def _one(self, task, dataset, plan_id, kind, seed, actions, cancel, *,
             on_event=None, run_index=1, total_runs=1):
        user, workspace = task["ownerId"], task["workspaceId"]
        duration = task["scenario"]["durationSeconds"]
        run = self.repo.create("runs", user, workspace, {"experimentId": task["id"], "planId": plan_id,
                               "experimentType": kind, "seed": seed, "status": "running",
                               'executionAttemptId': task.get('executionAttemptId'),
                               "datasetVersion": task["datasetVersion"], "scenario": task["scenario"]})
        active = {"runId": run["id"], "planId": plan_id, "experimentType": kind, "seed": seed,
                  "runIndex": run_index, "totalRuns": total_runs, "stepSeconds": 0,
                  "durationSeconds": duration, "progress": 0, "phase": "preparing", "personCount": 0}

        def publish(phase=None, update=None, *, persist=False, reset_frame=False):
            update = update or {}
            if phase:
                active["phase"] = phase
            if "stepSeconds" in update:
                active["stepSeconds"] = update["stepSeconds"]
                active["progress"] = min(1, max(0, update["stepSeconds"] / duration))
            frame = update.get("frame")
            if frame is not None:
                active["personCount"] = frame["totalPersons"]
            changes = {"activeRun": active}
            if "frame" in update or reset_frame:
                changes["frame"] = frame
            changes["frameError"] = update.get("frameError")
            self._publish(task["id"], **changes)
            if persist and on_event is not None:
                on_event({"status": "running", "activeRun": deepcopy(active)})

        archive, completed_run = None, None
        folder = self.data_dir / task["id"] / run["id"]
        try:
            publish("preparing", persist=True, reset_frame=True)
            archive = self.repo.begin_archive(run["id"], kind, user, workspace, {**run, "interventions": actions})
            folder.mkdir(parents=True, exist_ok=False)
            routes, fcd, trips = folder / "demand.rou.xml", folder / "fcd.xml", folder / "tripinfo.xml"
            manifest = compile_routes(dataset, seed, actions, duration, routes)
            (folder / "input.json").write_text(json.dumps({"scenario": task["scenario"], "seed": seed,
                "datasetVersion": task["datasetVersion"], "interventions": actions, **manifest}, ensure_ascii=False, indent=2), encoding="utf-8")
            command = [self.binary, "--net-file", str(NET_FILE), "--route-files", str(routes),
                       "--begin", "0", "--end", str(duration), "--step-length", "0.5", "--seed", str(seed),
                       "--pedestrian.model", "striping", "--time-to-teleport", "-1", "--no-step-log", "true",
                       "--pedestrian.striping.jamtime", str(duration + 1),
                       "--pedestrian.striping.jamtime.crossing", str(duration + 1),
                       "--pedestrian.striping.jamtime.narrow", str(duration + 1),
                       "--duration-log.disable", "true", "--fcd-output", str(fcd),
                       "--person-device.fcd.probability", "1", "--person-device.fcd.period", str(SAMPLE_SECONDS),
                       "--device.fcd.period", str(SAMPLE_SECONDS),
                       "--tripinfo-output", str(trips), "--tripinfo-output.write-unfinished", "true"]
            from postanalysis_api.services.factual_context import is_spatial
            if is_spatial(dataset):
                from .distribution_demand import sumo_parameters
                context = dataset['metadata']['factualContext']
                overrides = sumo_parameters(context) + [
                    '--person-device.fcd.period', str(context['samplingSeconds']),
                    '--device.fcd.period', str(context['samplingSeconds']), '--precision', '8']
                for key, value in zip(overrides[::2], overrides[1::2]):
                    if key in command:
                        command[command.index(key) + 1] = value
                    else:
                        command.extend([key, value])
            (folder / "command.json").write_text(json.dumps(command, indent=2), encoding="utf-8")
            command += ["--log", str(folder / "sumo.log")]
            actual_command = [self.binary, "-c", str(folder / "post.sumocfg"), "--start", "--quit-on-end", *command[5:]]
            if hasattr(self.repo, "archive_execution_inputs"):
                self.repo.archive_execution_inputs(archive, (folder / "input.json").read_bytes(), routes.read_bytes(), actual_command)
            publish("running")
            self._checkpoint(task, on_event, cancel)
            control_events = execute(self.binary, routes, folder, command[5:], manifest, duration, cancel.is_set,
                                     on_progress=lambda update: publish("running", update),
                                     before_step=lambda: self._checkpoint(task, on_event, cancel))
            self._checkpoint(task, on_event, cancel)
            publish("archiving", persist=True)
            if is_spatial(dataset):
                from .distribution_measurement import measure_distribution
                output = measure_distribution(fcd, duration, archive, self.repo, dataset, cancelled=cancel.is_set,
                                              control_events=control_events)
            else:
                output = measure(fcd, duration, archive, self.repo, dataset["observations"], cancelled=cancel.is_set)
            arrived = []
            for element in ET.parse(trips).getroot().findall("personinfo"):
                walks = element.findall("walk")
                if walks and len(walks) >= 2 and float(walks[-1].get("arrival", "-1")) >= 0:
                    arrived.append(float(walks[-1].get("arrival")))
            censored = len(arrived) < manifest["cohortCount"]
            output["metrics"]["evacuation_time"] = None if censored or not arrived else max(arrived)
            output["measurement"].update(evacuationCensored=censored, arrivedCount=len(arrived),
                                         metricDefinitionVersion='during-spatial-metrics/2.0' if is_spatial(dataset) else METRIC_VERSION,
                                         plannedPersonCount=manifest["cohortCount"])
            feature_values = {}
            fields = {"peak_density": "density", "mean_speed": "meanSpeed", "peak_pressure_proxy": "pressureProxy"}
            if is_spatial(dataset):
                fields['peak_density'] = 'peakDensity'
            for feature in task.get("pcFeatures", []):
                rows = output["regionSamples"].get(feature["regionId"], [])
                values = [row.get(fields.get(feature["metricId"])) for row in rows
                          if feature["windowStartSeconds"] <= row["timeSeconds"] < feature["windowEndSeconds"]]
                values = [value for value in values if isinstance(value, (int, float))]
                feature_values[feature["id"]] = (max(values) if feature["aggregation"] == "peak" else
                    sum(values) / len(values) if feature["aggregation"] == "mean" else values[-1]) if values else None
            output["featureValues"] = feature_values
            output.pop("regionSamples", None)
            output.update(status="completed", archive=archive, controls=manifest["controls"],
                          controlEvents=control_events,
                          executionEvidence={"assignedExitCounts": dict(Counter(person["exitRegionId"] for person in manifest["cohorts"])),
                                             "actualEventCounts": dict(Counter(event["type"] for event in control_events))},
                          assumptions=manifest["assumptions"], outputDirectory=str(folder))
            self.repo.finish_archive(archive, "completed", output)
            completed_run = self.repo.update("runs", run["id"], user, workspace, output)
            publish("completed", persist=True)
            return completed_run
        except Exception as exc:
            if completed_run is not None:
                # A late lifecycle callback cannot turn an already committed run
                # into a failed attempt. Resume will recover this group/seed.
                raise
            status = "cancelled" if isinstance(exc, InterruptedError) else "failed"
            if archive:
                self.repo.finish_archive(archive, status, {"error": str(exc)[:2000], "outputDirectory": str(folder)})
            self.repo.update("runs", run["id"], user, workspace, {"status": status, "archive": archive, "error": str(exc)[:2000]})
            publish(status)
            raise
