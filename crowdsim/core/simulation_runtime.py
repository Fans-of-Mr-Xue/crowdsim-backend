"""Top-level simulation lifecycle and deterministic tick boundary."""

from __future__ import annotations

from dataclasses import replace
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional
import uuid
import xml.etree.ElementTree as ET

from crowdsim.decision.agent_decision import AgentDecisionEngine
from crowdsim.decision.decision_scheduler import DecisionScheduler
from crowdsim.decision.plan_executor import PlanExecutor
from crowdsim.decision.route_provider import RouteProvider
from crowdsim.domain.crowdsim_models import AgentProfile, GroupRecord
from crowdsim.domain.group_manager import GroupManager
from crowdsim.environment.activity_planner import ActivityPlanner
from crowdsim.environment.crowd_environment import CrowdEnvironment
from crowdsim.environment.event_catalog import event_defaults
from crowdsim.environment.event_manager import EventManager
from crowdsim.environment.hazard_model import HazardModel, HazardZone
from crowdsim.environment.information_model import InformationModel
from crowdsim.environment.intervention_executor import InterventionExecutor
from crowdsim.environment.poi_catalog import PoiCatalog
from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder
from crowdsim.infrastructure.frame_serializer import FrameSerializer
from crowdsim.infrastructure.metrics import MetricsCollector
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.infrastructure.sumo_adapter import SumoAdapter, SumoStepResult
from crowdsim.core.population_manager import PopulationManager
from crowdsim.core.runtime_commands import RuntimeCommand, RuntimeCommandQueue
from crowdsim.core.state_updater import StateUpdater


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RuntimeState(str, Enum):
    CREATED = "CREATED"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"
    ERROR = "ERROR"
    CLOSED = "CLOSED"


class SimulationRuntime:
    """The only application component that advances simulation time."""

    def __init__(
        self,
        config_path: str | Path,
        *,
        pedestrian_route_files: Iterable[str | Path] = (),
        sumo_binary: str | None = None,
        extra_sumo_args: Optional[Iterable[str]] = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self._pedestrian_route_files = tuple(Path(path).resolve() for path in pedestrian_route_files)
        self._sumo_binary = sumo_binary
        self._extra_sumo_args = tuple(extra_sumo_args or ())
        self.run_id = f"run-{uuid.uuid4().hex}"
        self.network: ResearchNetwork | None = None
        self.adapter = SumoAdapter(
            self.config_path,
            sumo_binary=sumo_binary,
            extra_args=self._extra_sumo_args,
        )
        self.population = PopulationManager(self._pedestrian_route_files)
        self.state = RuntimeState.CREATED
        self.current: SumoStepResult | None = None
        self.snapshot_index = 0
        self.last_error: str | None = None
        self.sim_speed_factor = 1.0
        self.push_fps = 8.0
        self.step_length = 0.5
        self.serializer = FrameSerializer()
        self.flood_points: list[dict] = []
        self.flooded_roads: list[dict] = []
        self.serialized_events: list[dict] = []
        self.event_state: dict = {"crowd_gathering": {"active": False, "decision": "", "step": -1}}
        self.active_policy = ""
        self.decision_engine = AgentDecisionEngine()
        self.decision_scheduler = DecisionScheduler(self.decision_engine)
        self.use_llm = False
        self.environment: CrowdEnvironment | None = None
        self.state_updater = StateUpdater()
        self.information = InformationModel()
        self.hazards = HazardModel()
        self.interventions = InterventionExecutor(self.information)
        self.event_manager: EventManager | None = None
        self.groups = GroupManager()
        self.route_provider: RouteProvider | None = None
        self.plan_executor: PlanExecutor | None = None
        self.observations = {}
        self.demand_count: int | None = None
        self.prepared_demand_path: Path | None = None
        self.metrics_collector: MetricsCollector | None = None
        self.latest_metrics: dict = {}
        self.recorder: ExperimentRecorder | None = None
        self.poi_catalog: PoiCatalog | None = None
        self.activity_planner: ActivityPlanner | None = None
        self.decision_candidates = {}
        self._route_candidate_cache = {}
        self.unreachable_candidate_pairs: set[tuple[str, str]] = set()
        self.commands = RuntimeCommandQueue()

    @property
    def time_seconds(self) -> float:
        return self.current.time_seconds if self.current else 0.0

    @property
    def snapshot_id(self) -> str:
        return f"{self.run_id}:{self.snapshot_index}"

    @property
    def center(self) -> tuple[float, float]:
        if self.network is None:
            raise RuntimeError("runtime is not initialized")
        return self.network.center

    @property
    def real_step_interval(self) -> float:
        return self.step_length / max(0.01, self.sim_speed_factor)

    @property
    def running(self) -> bool:
        return self.state == RuntimeState.RUNNING

    def initialize(self) -> SumoStepResult:
        if self.state not in {RuntimeState.CREATED}:
            raise RuntimeError(f"initialize is invalid in state {self.state.value}")
        try:
            self.step_length = self._config_float("time", "step-length", 0.5)
            run_directory = PROJECT_ROOT / "runs" / self.run_id
            run_directory.mkdir(parents=True, exist_ok=True)
            if "--log" not in self.adapter.extra_args:
                self.adapter.extra_args.extend(["--log", str(run_directory / "sumo.log")])
            self.network = ResearchNetwork(str(self._net_path_from_config()))
            self.environment = CrowdEnvironment(self.network)
            self.event_manager = EventManager(self.network)
            poi_path = PROJECT_ROOT / "config" / "pois.json"
            if self.config_path.name.startswith("bund.") and poi_path.is_file():
                self.poi_catalog = PoiCatalog(self.network, poi_path)
                self.activity_planner = ActivityPlanner(self.poi_catalog)
            if self.population.ledger.planned_ids:
                self.prepared_demand_path = PROJECT_ROOT / "runs" / self.run_id / "demand.rou.xml"
                self.population.prepare_demand(self.prepared_demand_path, self.demand_count)
                # Replay and specialised experiments may already provide a complete
                # route-file override.  SUMO rejects duplicate occurrences of this
                # option, so only construct the normal override when none exists.
                if "--route-files" not in self.adapter.extra_args:
                    route_files = [path for path in self._route_files_from_config() if path not in self.population.route_files]
                    route_files.append(self.prepared_demand_path)
                    self.adapter.extra_args.extend(["--route-files", ",".join(str(path) for path in route_files)])
            self.current = self.adapter.start()
            self.population.reconcile(self.current)
            self.route_provider = RouteProvider(self.network, self.adapter)
            self.plan_executor = PlanExecutor(self.adapter, self.route_provider)
            self.metrics_collector = MetricsCollector(self.network)
            self.latest_metrics = self.metrics_collector.measure(self.current, self.population.states, self.population.diagnostics())
            self.recorder = ExperimentRecorder(self.run_id, self.config_path, self.adapter.diagnostics)
            demand_source = self.prepared_demand_path or (self.population.route_files[0] if len(self.population.route_files) == 1 else None)
            if demand_source is not None:
                self.recorder.archive_demand(demand_source)
            self.recorder.update_manifest(
                mode="llm" if self.use_llm else "rule",
                model_id=self.decision_engine.model if self.use_llm else None,
                step_length=self.step_length,
                profile_seed=self.population.profile_sampler.seed,
                pedestrian_route_files=[str(path) for path in self.population.route_files],
            )
            self.state = RuntimeState.READY
            return self.current
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.state = RuntimeState.ERROR
            self.adapter.close()
            raise

    def start(self) -> None:
        if self.state not in {RuntimeState.READY, RuntimeState.PAUSED}:
            raise RuntimeError(f"start is invalid in state {self.state.value}")
        self.state = RuntimeState.RUNNING

    def pause(self) -> None:
        if self.state != RuntimeState.RUNNING:
            raise RuntimeError(f"pause is invalid in state {self.state.value}")
        self.state = RuntimeState.PAUSED

    def tick(self) -> SumoStepResult:
        if self.state != RuntimeState.RUNNING:
            raise RuntimeError(f"tick is invalid in state {self.state.value}")
        try:
            due = self._prepare_boundary()
            plans = self.decision_scheduler.resolve_rule(due, self.population.profiles, self.population.states, self.observations, self.decision_candidates)
            return self._step_after_plans(plans)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.state = RuntimeState.ERROR
            self.adapter.close()
            raise

    async def tick_async(self) -> SumoStepResult:
        if self.state != RuntimeState.RUNNING:
            raise RuntimeError(f"tick is invalid in state {self.state.value}")
        try:
            due = self._prepare_boundary()
            plans = await self.decision_scheduler.resolve(due, self.population.profiles, self.population.states, self.observations, use_llm=self.use_llm, candidates=self.decision_candidates)
            if self.state != RuntimeState.RUNNING:
                return self.current
            return self._step_after_plans(plans)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.state = RuntimeState.ERROR
            self.adapter.close()
            raise

    def _prepare_boundary(self) -> list[str]:
        self.process_pending_commands()
        self.hazards.update(self.time_seconds)
        if self.event_manager is not None:
            self.event_manager.step(self.time_seconds)
            self.serialized_events = self.event_manager.serialize(self.time_seconds)
            for event in self.event_manager.events:
                if event.id in self.hazards.zones:
                    self.hazards.zones[event.id].intensity = event.intensity
        if self.current is not None:
            self.information.expire(self.time_seconds, self.population.states)
            deliveries = self.information.deliver(self.time_seconds, self.current.persons, self.population.profiles, self.population.states)
            if self.recorder is not None:
                self.recorder.record_messages(deliveries)
        if self.plan_executor is not None:
            self.plan_executor.maintain(self.time_seconds)
            for person_id, state in self.population.states.items():
                if state.planned_wait_until is not None and person_id not in self.plan_executor.wait_until:
                    state.planned_wait_until = None
            if self.current is not None:
                for person_id, motion in self.current.persons.items():
                    profile = self.population.profile_for(person_id)
                    base_speed = profile.free_walking_speed * profile.mobility
                    self.plan_executor.set_hazard_limit(person_id, self.hazards.speed_limit_for(motion, base_speed))
        return self.decision_scheduler.collect_due(self.population.states, self.observations, self.time_seconds)

    def queue_command(self, action: str, payload: dict, request_id=None) -> dict:
        command = RuntimeCommand(
            action=action,
            payload=dict(payload),
            request_id=None if request_id is None else str(request_id),
            submitted_at=self.time_seconds,
            submitted_snapshot=self.snapshot_id,
        )
        self.commands.submit(command)
        return {
            "type": "command_result",
            "request_id": request_id,
            "action": action,
            "status": "queued",
            "submitted_at": self.time_seconds,
            "snapshot_id": self.snapshot_id,
        }

    def process_pending_commands(self) -> list[dict]:
        results = self.commands.apply_all(
            self._apply_queued_command,
            applied_at=self.time_seconds,
            snapshot_id=self.snapshot_id,
        )
        if self.recorder is not None:
            for result in results:
                self.recorder.record_command(
                    {"action": result["action"], "request_id": result["request_id"]},
                    result,
                )
        return results

    def take_command_results(self) -> list[dict]:
        return self.commands.take_results()

    def _apply_queued_command(self, action: str, data: dict):
        if action == "update_flood_source":
            return self.set_flood(data)
        if action in {"set_event", "trigger_event"}:
            return self.set_event(data)
        if action in {"event_decision", "set_policy", "apply_policy"}:
            return self.apply_policy(data)
        if action == "set_group":
            record = GroupRecord(
                group_id=str(data.get("group_id") or ""),
                member_ids=[str(item) for item in data.get("member_ids", ())],
                leader_id=str(data["leader_id"]) if data.get("leader_id") is not None else None,
                rendezvous_id=str(data["rendezvous_id"]) if data.get("rendezvous_id") is not None else None,
            )
            if not record.group_id:
                raise ValueError("group_id is required")
            if record.rendezvous_id and (self.poi_catalog is None or record.rendezvous_id not in self.poi_catalog.pois):
                raise ValueError("rendezvous_id must reference a configured POI")
            self.groups.register(record, self.population.ledger.planned_ids | self.population.ledger.departed_ids)
            self.groups.update_groups(self.population.states)
            return {"group_id": record.group_id, "member_count": len(record.member_ids)}
        raise ValueError(f"unsupported queued command: {action}")

    def _step_after_plans(self, plans) -> SumoStepResult:
        if self.current is not None and self.plan_executor is not None:
            for plan in plans:
                if plan.person_id in self.current.persons:
                    self.apply_plan(plan)
        result = self.adapter.step()
        self.population.reconcile(result)
        for person_id in result.persons:
            if self.plan_executor is not None and person_id not in self.plan_executor.base_limits:
                self.plan_executor.register_profile(self.population.profile_for(person_id))
        old_states = dict(self.population.states)
        if self.environment is not None:
            observations = self.environment.observe(result.persons, self.population.profiles, old_states, f"{self.run_id}:{self.snapshot_index + 1}", self.hazards.zones.values())
            new_states = self.state_updater.update_all(old_states, self.population.profiles, observations, self.step_length)
            self.population.commit_states(new_states)
            self.observations = observations
            self._refresh_agent_context()
            self.groups.update_groups(self.population.states)
        self.current = result
        self.snapshot_index += 1
        if self.metrics_collector is not None:
            self.latest_metrics = self.metrics_collector.measure(result, self.population.states, self.population.diagnostics())
        if self.recorder is not None:
            self.recorder.record_step(self, result, self.latest_metrics)
        if self.adapter.min_expected_number <= 0:
            self.state = RuntimeState.FINISHED
        return result

    def _refresh_agent_context(self) -> None:
        self.decision_candidates = {}
        if self.activity_planner is None or self.poi_catalog is None or self.route_provider is None:
            return
        for person_id, observation in list(self.observations.items()):
            state = self.population.states[person_id]
            profile = self.population.profile_for(person_id)
            self.activity_planner.initialize(profile, state)
            known_ids = set(state.activity_plan)
            if state.rendezvous_id:
                known_ids.add(state.rendezvous_id)
            available = tuple(item["id"] for item in self.poi_catalog.available(self.time_seconds, known_ids))
            self.observations[person_id] = replace(observation, available_goal_ids=available)
            candidates = []
            for target_id in available:
                target = self.poi_catalog.pois[target_id]
                next_id = next((item for item in state.activity_plan if item != target_id), None)
                next_target = self.poi_catalog.pois.get(next_id) if next_id else None
                cache_key = (observation.own_motion.edge_id, target_id, next_id)
                if cache_key not in self._route_candidate_cache:
                    try:
                        self._route_candidate_cache[cache_key] = self.route_provider.build_activity_candidate(observation.own_motion, target, next_target)
                    except ValueError:
                        self._route_candidate_cache[cache_key] = None
                        self.unreachable_candidate_pairs.add((observation.own_motion.edge_id, target_id))
                candidate = self._route_candidate_cache[cache_key]
                if candidate is not None:
                    candidates.append(candidate)
            self.decision_candidates[person_id] = tuple(candidates)

    def close(self) -> None:
        if self.state == RuntimeState.CLOSED:
            return
        self.state = RuntimeState.CLOSED
        if self.recorder is not None:
            self.recorder.finalize(self)
        self.adapter.close()

    def set_playback(self, *, speed_factor=None, push_fps=None) -> None:
        if isinstance(speed_factor, (int, float)) and speed_factor > 0:
            self.sim_speed_factor = max(0.05, min(20.0, float(speed_factor)))
        if isinstance(push_fps, (int, float)) and push_fps > 0:
            self.push_fps = max(1.0, min(60.0, float(push_fps)))

    def configure_demand(self, count) -> None:
        if self.state != RuntimeState.CREATED:
            raise RuntimeError("count can only be changed before SUMO initialization; use reset")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("count must be a non-negative integer")
        if not self.population.ledger.planned_ids:
            raise ValueError("count requires a demand file with explicit <person> elements")
        self.demand_count = count

    def reset(self, count=None):
        config_path = self.config_path
        route_files = self._pedestrian_route_files
        sumo_binary = self._sumo_binary
        extra_args = self._extra_sumo_args
        self.close()
        self.__init__(config_path, pedestrian_route_files=route_files, sumo_binary=sumo_binary, extra_sumo_args=extra_args)
        if count is not None:
            self.configure_demand(count)
        return self.initialize()

    def frame(self) -> dict:
        return self.serializer.build_frame(self)

    def init_frame(self) -> dict:
        return self.serializer.build_init(self)

    def apply_plan(self, plan):
        if self.current is None or self.plan_executor is None:
            raise RuntimeError("runtime is not initialized")
        motion = self.current.persons.get(plan.person_id)
        if motion is None:
            raise ValueError(f"person is not active: {plan.person_id}")
        result = self.plan_executor.apply(plan, motion, self.snapshot_id, self.time_seconds)
        if result.status == "applied":
            state = self.population.states[plan.person_id]
            state.current_plan = plan
            if plan.proposed_action == "wait":
                state.planned_wait_until = plan.wait_until
            elif plan.proposed_action == "continue":
                state.planned_wait_until = None
            hold_until = plan.wait_until or (self.time_seconds + self.decision_scheduler.period_seconds)
            state.next_decision_time = max(state.next_decision_time, hold_until)
        if self.recorder is not None:
            self.recorder.record_decision(
                plan,
                result,
                context={
                    "profile": self.population.profile_for(plan.person_id),
                    "state": self.population.states.get(plan.person_id),
                    "observation": self.observations.get(plan.person_id),
                },
                candidates=self.decision_candidates.get(plan.person_id, ()),
            )
        return result

    @property
    def decision_diagnostics(self) -> dict:
        return self.decision_scheduler.diagnostics()

    def profile_for(self, person_id: str) -> AgentProfile:
        return self.population.profile_for(person_id)

    def local_people_count(self, person_id: str) -> int:
        observation = self.observations.get(person_id)
        return observation.local_people_count if observation else 0

    def local_density_for(self, person_id: str):
        observation = self.observations.get(person_id)
        return observation.objective_density_per_m2 if observation else None

    def density_level_for(self, person_id: str) -> str:
        observation = self.observations.get(person_id)
        return self.environment.classify_density(observation.objective_density_per_m2) if observation and self.environment else "unknown"

    def density_level_counts(self) -> dict:
        result = {"free": 0, "busy": 0, "crowded": 0, "critical": 0, "unknown": 0}
        for person_id in self.current.persons if self.current else ():
            result[self.density_level_for(person_id)] += 1
        return result

    def congestion_for(self, person_id: str) -> float:
        return 0.0

    def hazard_impact_for(self, person_id: str) -> float:
        motion = self.current.persons.get(person_id) if self.current else None
        return self.hazards.impact_for(motion) if motion else 0.0

    def set_flood(self, data: dict) -> None:
        if data.get("mode") == "clear":
            self.hazards.replace_type("flood", ())
        elif data.get("mode") == "static_points":
            if self.network is None:
                raise RuntimeError("runtime is not initialized")
            zones = []
            for index, item in enumerate(data.get("points", ())):
                lon = float(item.get("lng", item.get("lon", item.get("longitude"))))
                lat = float(item.get("lat", item.get("latitude")))
                x, y = self.network.lonlat_to_xy(lon, lat)
                zones.append(HazardZone(str(item.get("id") or f"flood-{index}"), "flood", x, y, max(1.0, float(item.get("radius", data.get("radius", 30)))), max(0.0, min(1.0, float(item.get("depth", item.get("currentDepth", 0.25))))), 0.75, lon=lon, lat=lat))
            self.hazards.replace_type("flood", zones)
        else:
            raise ValueError("only static_points and clear flood modes are supported")
        self.flood_points = self.hazards.serialized_points()

    def set_event(self, data: dict) -> None:
        if self.event_manager is None:
            raise RuntimeError("runtime is not initialized")
        before_ids = {event.id for event in self.event_manager.events}
        event = self.event_manager.apply(data, self.time_seconds, self.snapshot_index)
        if event is None:
            after_ids = {item.id for item in self.event_manager.events}
            for removed_id in before_ids - after_ids:
                self.hazards.zones.pop(removed_id, None)
            self.serialized_events = self.event_manager.serialize(self.time_seconds)
            return
        defaults = event_defaults(event.event_type)
        if defaults["physical"]:
            self.hazards.add(HazardZone(event.id, event.event_type, event.x, event.y, event.radius, event.intensity, float(defaults["speed_reduction"]), event.expires_at))
        if event.event_type in {"alarm", "rumor"}:
            from crowdsim.environment.information_model import InformationMessage
            self.information.publish(InformationMessage(f"message-{event.id}", str(data.get("message") or event.event_type), str(defaults["message_source"]), self.time_seconds, self.time_seconds + max(0.0, float(data.get("messageDelay", 0))), event.expires_at, event.x, event.y, event.radius, event.id))
        self.serialized_events = self.event_manager.serialize(self.time_seconds)

    def apply_policy(self, data: dict) -> dict:
        name = str(data.get("decision") or data.get("policy") or data.get("name") or "")
        record = self.interventions.apply_command(name, data, self.time_seconds)
        self.active_policy = name
        self.event_state = {"crowd_gathering": {"active": bool(name), "decision": name, "step": self.snapshot_index}}
        return record

    def _config_root(self):
        return ET.parse(self.config_path).getroot()

    def _config_float(self, section: str, name: str, default: float) -> float:
        element = self._config_root().find(f"./{section}/{name}")
        try:
            return float(element.get("value")) if element is not None else default
        except (TypeError, ValueError):
            return default

    def _net_path_from_config(self) -> Path:
        element = self._config_root().find("./input/net-file")
        if element is None or not element.get("value"):
            raise ValueError(f"No net-file in {self.config_path}")
        return (self.config_path.parent / element.get("value")).resolve()

    def _route_files_from_config(self) -> list[Path]:
        element = self._config_root().find("./input/route-files")
        if element is None:
            return []
        return [(self.config_path.parent / value.strip()).resolve() for value in element.get("value", "").split(",") if value.strip()]

    def diagnostics(self) -> dict:
        return {
            "state": self.state.value,
            "snapshot_index": self.snapshot_index,
            "time_seconds": self.time_seconds,
            "last_error": self.last_error,
            "engine": self.adapter.diagnostics,
            "population": self.population.diagnostics(),
            "routing": {"cached_candidates": len(self._route_candidate_cache), "unreachable_pairs": len(self.unreachable_candidate_pairs)},
        }

    def __enter__(self) -> "SimulationRuntime":
        self.initialize()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
