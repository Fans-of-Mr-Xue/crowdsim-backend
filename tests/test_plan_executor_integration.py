from pathlib import Path
import unittest

from crowdsim.domain.crowdsim_models import AgentProfile, BehaviorPlan
from crowdsim.decision.plan_executor import PlanExecutor
from crowdsim.decision.route_provider import RouteProvider
from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.infrastructure.sumo_adapter import discover_sumo_binary


ROOT = Path(__file__).resolve().parents[1]


def has_sumo() -> bool:
    try:
        discover_sumo_binary()
        return True
    except FileNotFoundError:
        return False


def running_runtime(name: str) -> SimulationRuntime:
    directory = ROOT / "tests" / "scenarios" / name
    runtime = SimulationRuntime(directory / "scenario.sumocfg", pedestrian_route_files=[directory / "demand.rou.xml"])
    runtime.initialize()
    runtime.start()
    return runtime


def first_person(runtime: SimulationRuntime):
    for _ in range(10):
        step = runtime.tick()
        if step.persons:
            return next(iter(step.persons.values()))
    raise AssertionError("no SUMO person departed")


@unittest.skipUnless(has_sumo(), "real SUMO binary is required")
class PlanExecutorIntegrationTests(unittest.TestCase):
    def test_route_candidate_comes_from_sumo_intermodal_routing(self):
        runtime = running_runtime("alternative_routes")
        try:
            motion = first_person(runtime)
            candidate = runtime.route_provider.build_candidate(motion, target_id="lower", target_edge="lower_b")
            self.assertEqual(motion.edge_id, candidate.edges[0])
            self.assertEqual("lower_b", candidate.edges[-1])
            self.assertGreater(candidate.estimated_cost_seconds, 0)
        finally:
            runtime.close()

    def test_wait_is_stationary_then_restores_motion(self):
        runtime = running_runtime("unidirectional_corridor")
        try:
            motion = first_person(runtime)
            executor = runtime.plan_executor
            executor.register_profile(AgentProfile(person_id=motion.person_id, free_walking_speed=1.35))
            plan = BehaviorPlan(person_id=motion.person_id, snapshot_id=runtime.snapshot_id, proposed_action="wait", wait_until=runtime.time_seconds + 5.0, decided_at=runtime.time_seconds)
            self.assertEqual("applied", runtime.apply_plan(plan).status)
            start = (motion.x, motion.y)
            for _ in range(10):
                stopped = runtime.tick().persons[motion.person_id]
            drift = ((stopped.x - start[0]) ** 2 + (stopped.y - start[1]) ** 2) ** 0.5
            self.assertLessEqual(drift, 0.01)
            executor.maintain(runtime.time_seconds)
            before = (stopped.x, stopped.y)
            for _ in range(4):
                resumed = runtime.tick().persons[motion.person_id]
            displacement = ((resumed.x - before[0]) ** 2 + (resumed.y - before[1]) ** 2) ** 0.5
            self.assertGreater(displacement, 0.01)
        finally:
            runtime.close()

    def test_reroute_changes_actual_sumo_stage_without_position_jump(self):
        runtime = running_runtime("alternative_routes")
        try:
            motion = first_person(runtime)
            executor = runtime.plan_executor
            executor.register_profile(AgentProfile(person_id=motion.person_id))
            alternative = ("entry", "lower_a", "lower_b", "exit")
            plan = BehaviorPlan(person_id=motion.person_id, snapshot_id=runtime.snapshot_id, proposed_action="reroute", target_id="lower", route_edges=alternative, reason="integration test")
            result = runtime.apply_plan(plan)
            self.assertEqual("applied", result.status, result.reason)
            actual_stage = runtime.adapter.current_person_stage(motion.person_id)
            self.assertEqual(alternative, tuple(actual_stage.edges))
            after = runtime.tick().persons[motion.person_id]
            displacement = ((after.x - motion.x) ** 2 + (after.y - motion.y) ** 2) ** 0.5
            self.assertLessEqual(displacement, 1.35 * runtime.step_length + 0.05)
            self.assertEqual(motion.person_id, after.person_id)
        finally:
            runtime.close()

    def test_activity_goal_waits_then_continues_to_exit_with_same_id(self):
        runtime = running_runtime("alternative_routes")
        try:
            motion = first_person(runtime)
            executor = runtime.plan_executor
            executor.register_profile(AgentProfile(person_id=motion.person_id))
            plan = BehaviorPlan(person_id=motion.person_id, snapshot_id=runtime.snapshot_id, proposed_action="change_goal", target_id="merge_activity", route_edges=("entry", "upper_a", "upper_b"), activity_duration=3.0, next_route_edges=("exit",), reason="activity lifecycle test")
            result = runtime.apply_plan(plan)
            self.assertEqual("applied", result.status, result.reason)
            self.assertEqual(3, runtime.adapter.remaining_stage_count(motion.person_id))
            saw_waiting = False
            seen_stages = set()
            for _ in range(220):
                step = runtime.tick()
                if motion.person_id not in step.persons:
                    break
                stage = runtime.adapter.current_person_stage(motion.person_id)
                seen_stages.add((runtime.adapter.remaining_stage_count(motion.person_id), stage.description))
                saw_waiting = saw_waiting or "activity:merge_activity" in stage.description
            self.assertTrue(saw_waiting, seen_stages)
            self.assertIn(motion.person_id, runtime.population.ledger.arrived_ids)
            self.assertNotIn(motion.person_id, runtime.population.ledger.unknown_disappearances)
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
