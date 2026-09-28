"""Exclusive owner of the TraCI connection and SUMO motion state."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import uuid
from typing import Dict, Iterable, Optional

import traci
from traci import constants as tc
from traci._simulation import Stage

from crowdsim.domain.crowdsim_models import MotionSnapshot


@dataclass(frozen=True)
class SumoStepResult:
    time_seconds: float
    persons: Dict[str, MotionSnapshot]
    vehicles: Dict[str, dict]
    departed_person_ids: tuple[str, ...]
    arrived_person_ids: tuple[str, ...]


def discover_sumo_binary(explicit: str | None = None) -> str:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if os.environ.get("SUMO_BINARY"):
        candidates.append(Path(os.environ["SUMO_BINARY"]))
    if os.environ.get("SUMO_HOME"):
        suffix = ".exe" if os.name == "nt" else ""
        candidates.append(Path(os.environ["SUMO_HOME"]) / "bin" / f"sumo{suffix}")
    located = shutil.which("sumo")
    if located:
        candidates.append(Path(located))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    raise FileNotFoundError("SUMO binary not found; set SUMO_HOME or SUMO_BINARY")


class SumoAdapter:
    """Own exactly one labelled TraCI connection.

    No other module imports or calls the process-global ``traci`` domains.
    """

    def __init__(
        self,
        config_path: str | Path,
        *,
        sumo_binary: str | None = None,
        extra_args: Optional[Iterable[str]] = None,
        label: str | None = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.sumo_binary = discover_sumo_binary(sumo_binary)
        self.extra_args = list(extra_args or ())
        self.label = label or f"crowdsim-{uuid.uuid4().hex}"
        self.connection = None
        self.started = False
        self.closed = False
        self.close_error: str | None = None
        self._version: str | None = None
        self._last_normal_edge: Dict[str, str] = {}

    def start(self) -> SumoStepResult:
        if self.started and not self.closed:
            raise RuntimeError("SUMO adapter is already started")
        if not self.config_path.is_file():
            raise FileNotFoundError(self.config_path)
        command = [
            self.sumo_binary,
            "-c",
            str(self.config_path),
            "--start",
            "--quit-on-end",
            *self.extra_args,
        ]
        try:
            traci.start(command, label=self.label)
            self.connection = traci.getConnection(self.label)
            self._version = self.connection.getVersion()[1]
            self.started = True
            self.closed = False
            self.close_error = None
            return self.snapshot()
        except Exception:
            self._close_after_failure()
            raise

    @property
    def time_seconds(self) -> float:
        self._require_connection()
        return float(self.connection.simulation.getTime())

    @property
    def diagnostics(self) -> dict:
        version = self._version
        if self.connection is not None and not self.closed:
            try:
                version = self.connection.getVersion()[1]
            except Exception:
                version = None
        return {
            "backend": "sumo",
            "pedestrian_model": "striping",
            "sumo_version": version,
            "config": str(self.config_path),
            "started": self.started,
            "closed": self.closed,
            "close_error": self.close_error,
        }

    @property
    def min_expected_number(self) -> int:
        self._require_connection()
        return int(self.connection.simulation.getMinExpectedNumber())

    def step(self) -> SumoStepResult:
        self._require_connection()
        try:
            self.connection.simulationStep()
            return self.snapshot(
                departed=self.connection.simulation.getDepartedPersonIDList(),
                arrived=self.connection.simulation.getArrivedPersonIDList(),
            )
        except Exception:
            self._close_after_failure()
            raise

    def advance_to(self, time_seconds: float) -> SumoStepResult:
        """Let SUMO execute all configured internal steps, then read one snapshot.

        This is intended for sparse offline measurement.  The interactive runtime
        must continue to call :meth:`step` at every decision boundary.
        """
        self._require_connection()
        target = float(time_seconds)
        if target <= self.time_seconds:
            raise ValueError("advance target must be later than current SUMO time")
        try:
            self.connection.simulationStep(target)
            return self.snapshot(
                departed=self.connection.simulation.getDepartedPersonIDList(),
                arrived=self.connection.simulation.getArrivedPersonIDList(),
            )
        except Exception:
            self._close_after_failure()
            raise

    def snapshot(
        self,
        *,
        departed: Iterable[str] = (),
        arrived: Iterable[str] = (),
    ) -> SumoStepResult:
        self._require_connection()
        time_seconds = float(self.connection.simulation.getTime())
        departed_set = set(departed)
        persons: Dict[str, MotionSnapshot] = {}
        for person_id in self.connection.person.getIDList():
            x, y = self.connection.person.getPosition(person_id)
            lon, lat = self._convert_geo(x, y)
            edge_id = self.connection.person.getRoadID(person_id)
            lane_id = self.connection.person.getLaneID(person_id)
            try:
                stage = self.connection.person.getStage(person_id)
                stage_type = int(stage.type)
            except Exception:
                stage_type = tc.STAGE_WAITING
            persons[person_id] = MotionSnapshot(
                person_id=person_id,
                time_seconds=time_seconds,
                x=float(x),
                y=float(y),
                lon=float(lon),
                lat=float(lat),
                speed=float(self.connection.person.getSpeed(person_id)),
                edge_id=edge_id,
                lane_id=lane_id,
                lane_position=float(self.connection.person.getLanePosition(person_id)),
                angle=float(self.connection.person.getAngle(person_id)),
                # TraCI 1.24 exposes the current stage as index 0 relative to
                # the remaining plan; it does not expose an absolute index.
                stage_index=0,
                stage_type=stage_type,
                departed=person_id in departed_set,
                remaining_stage_count=int(self.connection.person.getRemainingStages(person_id)),
            )
            if edge_id and not edge_id.startswith(":"):
                self._last_normal_edge[person_id] = edge_id
        vehicles: Dict[str, dict] = {}
        for vehicle_id in self.connection.vehicle.getIDList():
            x, y = self.connection.vehicle.getPosition(vehicle_id)
            lon, lat = self._convert_geo(x, y)
            vehicles[vehicle_id] = {
                "id": vehicle_id,
                "x": float(x),
                "y": float(y),
                "lon": float(lon),
                "lat": float(lat),
                "speed": float(self.connection.vehicle.getSpeed(vehicle_id)),
                "edge_id": self.connection.vehicle.getRoadID(vehicle_id),
            }
        return SumoStepResult(
            time_seconds=time_seconds,
            persons=persons,
            vehicles=vehicles,
            departed_person_ids=tuple(departed),
            arrived_person_ids=tuple(arrived),
        )

    def display_edge(self, person_id: str, edge_id: str) -> str:
        return self._last_normal_edge.get(person_id, edge_id) if edge_id.startswith(":") else edge_id

    def set_person_speed(self, person_id: str, speed_limit: float) -> None:
        self._require_connection()
        self.connection.person.setSpeed(person_id, float(speed_limit))

    def current_person_stage(self, person_id: str) -> Stage:
        self._require_connection()
        return self.connection.person.getStage(person_id)

    def remaining_stage_count(self, person_id: str) -> int:
        self._require_connection()
        return int(self.connection.person.getRemainingStages(person_id))

    def remaining_person_stages(self, person_id: str) -> tuple[Stage, ...]:
        return tuple(self.connection.person.getStage(person_id, index)
                     for index in range(self.remaining_stage_count(person_id)))

    def remove_future_person_stages(self, person_id: str) -> None:
        # Never remove index 0: removing the current stage moves the person.
        for index in range(self.remaining_stage_count(person_id) - 1, 0, -1):
            self.connection.person.removeStage(person_id, index)

    def anchor_current_person_stage(self, person_id: str) -> None:
        """Keep the person at its current position between same-boundary writes.

        SUMO retains a zero-duration waiting stage when the last stage is
        removed. No simulationStep may occur until the walking stage is restored.
        This avoids SUMO 1.27's convertTraCIStage using the old goal's arrivalPos
        as a new walk's initial departPos (it ignores Stage.departPos).
        """
        self._require_connection()
        if self.remaining_stage_count(person_id) != 1:
            raise ValueError("anchoring requires future stages to be removed first")
        self.connection.person.removeStage(person_id, 0)
        if self.remaining_stage_count(person_id) != 1:
            raise RuntimeError("SUMO did not retain the temporary position anchor")

    def append_person_stage(self, person_id: str, stage: Stage) -> None:
        self.connection.person.appendStage(person_id, stage)

    def replace_current_person_stage(self, person_id: str, stage: Stage) -> None:
        self._require_connection()
        self.connection.person.replaceStage(person_id, 0, stage)

    def append_waiting_stage(self, person_id: str, duration: float, description: str) -> None:
        self._require_connection()
        self.connection.person.appendWaitingStage(person_id, float(duration), description)

    def append_walking_stage(self, person_id: str, edges: Iterable[str], arrival_position: float) -> None:
        self._require_connection()
        self.connection.person.appendWalkingStage(person_id, list(edges), float(arrival_position))

    def find_pedestrian_route(
        self,
        from_edge: str,
        to_edge: str,
        *,
        depart_pos: float = 0.0,
        arrival_pos: float = tc.INVALID_DOUBLE_VALUE,
    ) -> tuple[Stage, ...]:
        self._require_connection()
        stages = self.connection.simulation.findIntermodalRoute(
            from_edge,
            to_edge,
            modes="",
            departPos=depart_pos,
            arrivalPos=arrival_pos,
        )
        return tuple(stages)

    def close(self) -> None:
        if self.closed:
            return
        connection = self.connection
        if connection is not None:
            try:
                connection.close(wait=True)
            except Exception as exc:
                # Keep the handle for a later cleanup attempt. A failed close
                # cannot establish that the SUMO process has exited.
                self.close_error = f"{type(exc).__name__}: {exc}"
                raise
            try:
                traci.removeConnection(self.label)
            except Exception:
                pass
        self.connection = None
        self.closed = True
        self.close_error = None

    def _close_after_failure(self) -> None:
        try:
            self.close()
        except Exception:
            # Preserve the startup error and the truthful close diagnostics.
            pass

    def _require_connection(self) -> None:
        if self.connection is None or self.closed:
            raise RuntimeError("SUMO adapter is not connected")

    def _convert_geo(self, x: float, y: float) -> tuple[float, float]:
        try:
            lon, lat = self.connection.simulation.convertGeo(x, y)
            return float(lon), float(lat)
        except traci.TraCIException:
            # Unit/integration networks intentionally use local Cartesian
            # coordinates. Production research networks must be projected.
            return float(x), float(y)

    def __enter__(self) -> "SumoAdapter":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
