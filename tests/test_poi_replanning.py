"""POI safety, caching, retry and stage-transaction regression tests."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from traci import constants as tc
from traci._simulation import Stage

from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.decision.agent_decision import AgentDecisionEngine
from crowdsim.decision.pedestrian_reachability import PedestrianReachability, resolve_position
from crowdsim.decision.plan_executor import PlanExecutor
from crowdsim.decision.route_provider import RouteCandidate, RouteProvider, RouteUnavailable
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, BehaviorPlan, MotionSnapshot, Observation
from crowdsim.infrastructure.network_adapter import ResearchNetwork


ROOT = Path(__file__).resolve().parents[1]


class Edge:
    def __init__(self, edge_id, start, end, length=20.0, allowed=True):
        self.edge_id, self.start, self.end, self.length = edge_id, start, end, length
        self.allowed = allowed

    def getID(self):
        return self.edge_id

    def getLength(self):
        return self.length

    def getLanes(self):
        return [SimpleNamespace(allows=lambda vclass: self.allowed, getLength=lambda: self.length)]

    def getFunction(self):
        return ""

    def getFromNode(self):
        return SimpleNamespace(getID=lambda: self.start)

    def getToNode(self):
        return SimpleNamespace(getID=lambda: self.end)

    def getAllowedOutgoing(self, vclass):
        return {}

    def getIncoming(self):
        return {}


class Network:
    def __init__(self):
        self.edges = {edge.getID(): edge for edge in (
            Edge("a", "0", "1", 40), Edge("b", "1", "2", 10),
            Edge("c", "2", "3", 30), Edge("highway", "2", "4", allowed=False),
            Edge("island", "4", "5"))}

    def pedestrian_edge_ids(self):
        return {key for key, edge in self.edges.items() if edge.allowed}


def motion(position=3.0, now=1.0):
    return MotionSnapshot("p", now, 0, 0, 0, 0, 1, "a", "a_0", position, 0, 0, tc.STAGE_WALKING)


class Adapter:
    def __init__(self):
        self.stages = [Stage(type=tc.STAGE_WALKING, edges=["a", "b", "c"], arrivalPos=29.99)]
        self.find_pedestrian_route = Mock(side_effect=lambda a, b, **kwargs: (
            Stage(type=tc.STAGE_WALKING, edges=[a, b] if a != b else [a], cost=100),))
        self.mutations = []
        self.fail_append = False
        self.fail_restore = False

    def remaining_person_stages(self, person_id):
        return tuple(deepcopy(self.stages))

    def remaining_stage_count(self, person_id):
        return len(self.stages)

    def remove_future_person_stages(self, person_id):
        self.mutations.append("remove_future")
        self.stages = self.stages[:1]

    def replace_current_person_stage(self, person_id, stage):
        self.mutations.append("replace")
        if self.fail_restore and len(self.mutations) > 3:
            raise RuntimeError("restore transport error")
        self.stages[0] = deepcopy(stage)

    def anchor_current_person_stage(self, person_id):
        self.mutations.append("anchor")
        self.stages = [Stage(type=tc.STAGE_WAITING, edges=[self.stages[0].edges[0]], arrivalPos=3)]

    def append_waiting_stage(self, person_id, duration, description):
        self.mutations.append("wait")
        self.stages.append(Stage(type=tc.STAGE_WAITING, edges=[self.stages[-1].edges[-1]],
                                 travelTime=duration, description=description))

    def append_walking_stage(self, person_id, edges, arrival_position):
        self.mutations.append("walk")
        if self.fail_append:
            self.fail_append = False
            raise RuntimeError("append transport error")
        self.stages.append(Stage(type=tc.STAGE_WALKING, edges=list(edges), arrivalPos=arrival_position))

    def append_person_stage(self, person_id, stage):
        self.mutations.append("restore_future")
        self.stages.append(deepcopy(stage))

    def set_person_speed(self, person_id, speed):
        pass


class RouteSafetyTests(unittest.TestCase):
    def setUp(self):
        self.network, self.adapter = Network(), Adapter()
        self.provider = RouteProvider(self.network, self.adapter)

    def test_forbidden_bridge_is_not_a_pedestrian_connection(self):
        self.assertFalse(self.provider.reachability.can_reach("a", "island"))
        with self.assertRaises(RouteUnavailable):
            self.provider.build_candidate(motion(), target_id="island", target_edge="island")
        self.adapter.find_pedestrian_route.assert_not_called()

    def test_reported_bund_warning_pairs_are_filtered_before_sumo(self):
        network = ResearchNetwork(str(ROOT / "scenarios/shanghai_bund/bund.net.xml"))
        provider = RouteProvider(network, self.adapter)
        pairs = [("-40784652#1", "987437453#1"), ("40040314#0", "987437453#1"),
                 ("-965004749#1", "792385181#2"), ("-965004749#1", "40301368#10")]
        for start, target in pairs:
            with self.subTest(start=start, target=target), self.assertRaises(RouteUnavailable):
                provider.build_candidate(replace(motion(), edge_id=start), target_id=target, target_edge=target)
        self.assertTrue(provider.reachability.can_reach("40301368#10", "987437453#1"))
        self.adapter.find_pedestrian_route.assert_not_called()

    def test_shared_geometry_not_shared_departure_cost(self):
        first = self.provider.build_candidate(motion(3), target_id="b", target_edge="b")
        second = self.provider.build_candidate(motion(15), target_id="b", target_edge="b")
        self.assertEqual(first.edges, second.edges)
        self.assertLess(second.estimated_cost_seconds, first.estimated_cost_seconds)
        self.adapter.find_pedestrian_route.assert_not_called()
        self.assertGreater(self.provider.position_router.diagnostics()["tree_cache_hits"], 0)
        self.assertAlmostEqual(9.99, second.arrival_position)

    def test_numeric_poi_position_used_in_both_legs(self):
        target = {"id": "museum", "kind": "activity", "edge": "b", "position": 4, "stay_seconds": [2, 4]}
        onward = {"id": "exit", "kind": "exit", "edge": "c", "position": 12}
        candidate = self.provider.build_activity_candidate(motion(), target, onward)
        self.adapter.find_pedestrian_route.assert_not_called()
        self.assertEqual((4, 12, "exit"), (candidate.arrival_position, candidate.next_arrival_position, candidate.next_target_id))

    def test_position_router_is_independent_of_traci_route_query_failures(self):
        self.adapter.find_pedestrian_route.side_effect = RuntimeError("temporary socket error")
        candidate = self.provider.build_candidate(motion(), target_id="b", target_edge="b")
        self.assertEqual(("a", "b"), candidate.edges)
        self.adapter.find_pedestrian_route.assert_not_called()

    def test_endpoint_trees_are_reused_across_exact_positions(self):
        self.provider.build_candidate(motion(3), target_id="b", target_edge="b")
        builds = self.provider.position_router.diagnostics()["tree_builds"]
        self.provider.build_candidate(motion(15), target_id="b", target_edge="b")
        self.assertEqual(builds, self.provider.position_router.diagnostics()["tree_builds"])

    def test_positions_rejected_locally(self):
        for position in (-1, 11, float("nan"), float("inf")):
            with self.subTest(position=position), self.assertRaises(ValueError):
                resolve_position(self.network, "b", position)
        self.assertLess(resolve_position(self.network, "b", "end"), 10)

    def test_activity_requires_reachable_onward_route(self):
        target = {"id": "museum", "kind": "activity", "edge": "b", "stay_seconds": [2, 3]}
        with self.assertRaises(RouteUnavailable):
            self.provider.build_activity_candidate(motion(), target, {"id": "bad-exit", "edge": "island"})
        with self.assertRaises(RouteUnavailable):
            self.provider.build_activity_candidate(motion(), target, None)

    def test_builds_route_through_required_hotspot_entrance(self):
        self.adapter.find_pedestrian_route.side_effect = lambda start, end, **kwargs: (
            Stage(type=tc.STAGE_WALKING, edges={
                ("a", "b"): ["a", "b"],
                ("b", "c"): ["b", "c"],
            }[(start, end)]),
        )

        candidate = self.provider.build_via_candidate(
            motion(), target_id="monument", target_edge="c", via_edge="b", arrival_position=12
        )

        self.assertEqual(("a", "b", "c"), candidate.edges)
        self.assertEqual("b", candidate.entry_edge)
        self.assertEqual("hotspot_route", candidate.target_kind)


class ExecutionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.adapter = Adapter()
        self.executor = PlanExecutor(self.adapter, RouteProvider(Network(), self.adapter))
        self.plan = BehaviorPlan("p", "s", "change_goal", target_id="museum", route_edges=("a", "b"),
                                 arrival_position=4, activity_duration=3, next_route_edges=("b", "c"),
                                 next_arrival_position=12, next_target_id="exit")

    def test_entire_plan_preflight_before_mutation(self):
        for plan in (replace(self.plan, arrival_position=11), replace(self.plan, next_arrival_position=31),
                     replace(self.plan, next_route_edges=()), replace(self.plan, activity_duration=float("nan"))):
            executor = PlanExecutor(self.adapter, self.executor.route_provider)
            result = executor.apply(plan, motion(), "s", 1)
            self.assertEqual("rejected", result.status)
            self.assertEqual([], self.adapter.mutations)

    def test_uses_new_target_position_not_old_arrival(self):
        result = self.executor.apply(self.plan, motion(), "s", 1)
        self.assertEqual("applied", result.status, result.reason)
        self.assertEqual(4, self.adapter.stages[0].arrivalPos)
        self.assertEqual(12, self.adapter.stages[-1].arrivalPos)

    def test_repeated_plan_replaces_future_stages_instead_of_duplicating(self):
        for now in (1, 3):
            self.assertEqual("applied", self.executor.apply(self.plan, motion(now=now), "s", now).status)
            self.assertEqual(3, len(self.adapter.stages))

    def test_hotspot_reroute_preserves_existing_wait_and_departure_stages(self):
        self.adapter.append_waiting_stage("p", 30, "hotspot_visit")
        self.adapter.append_walking_stage("p", ("c",), 20)
        future = deepcopy(self.adapter.stages[1:])
        plan = BehaviorPlan(
            "p", "s", "reroute", target_id="monument", route_edges=("a", "b", "c"),
            arrival_position=29.99, selected_entry_edge="b", preserve_future_stages=True,
        )

        result = self.executor.apply(plan, motion(), "s", 1)

        self.assertEqual("applied", result.status, result.reason)
        self.assertEqual(3, len(self.adapter.stages))
        self.assertEqual([stage.type for stage in future], [stage.type for stage in self.adapter.stages[1:]])
        self.assertEqual([stage.edges for stage in future], [stage.edges for stage in self.adapter.stages[1:]])
        self.assertEqual(29.99, self.adapter.stages[0].arrivalPos)

    def test_hotspot_reroute_rejects_changed_target_position_before_mutation(self):
        self.adapter.append_waiting_stage("p", 30, "hotspot_visit")
        self.adapter.append_walking_stage("p", ("c",), 20)
        plan = BehaviorPlan(
            "p", "s", "reroute", target_id="monument", route_edges=("a", "b", "c"),
            arrival_position=12, selected_entry_edge="b", preserve_future_stages=True,
        )

        result = self.executor.apply(plan, motion(), "s", 1)

        self.assertEqual("rejected", result.status)
        self.assertIn("original target position", result.reason)
        self.assertEqual([], self.adapter.mutations[2:])

    def test_failed_append_restores_original_itinerary(self):
        self.adapter.append_waiting_stage("p", 5, "original-wait")
        self.adapter.append_walking_stage("p", ("c",), 20)
        original = deepcopy(self.adapter.stages)
        self.adapter.fail_append = True
        result = self.executor.apply(self.plan, motion(), "s", 1)
        self.assertEqual("rejected", result.status, result.reason)
        self.assertEqual([stage.edges for stage in original], [stage.edges for stage in self.adapter.stages])
        self.assertEqual([stage.arrivalPos for stage in original], [stage.arrivalPos for stage in self.adapter.stages])
        self.assertEqual(1, self.executor.diagnostics["itinerary_rollbacks"])

    def test_failed_restoration_is_not_reported_as_continue_success(self):
        self.adapter.fail_append = self.adapter.fail_restore = True
        result = self.executor.apply(self.plan, motion(), "s", 1)
        self.assertEqual("partial_failure", result.status, result.reason)
        self.assertEqual("unknown", result.applied_action)

    def test_internal_edge_and_waiting_stage_are_deferred(self):
        for m in (replace(motion(), edge_id=":junction"), replace(motion(), stage_type=tc.STAGE_WAITING),
                  replace(motion(), lane_position=41), replace(motion(), lane_position=float("nan"))):
            executor = PlanExecutor(self.adapter, self.executor.route_provider)
            self.assertEqual("rejected", executor.apply(self.plan, m, "s", 1).status)
        self.assertEqual([], self.adapter.mutations)

    def test_retry_cooldown_does_not_block_continue(self):
        self.executor.defer_route("p", 1, "no route")
        self.assertFalse(self.executor.route_ready("p", 10))
        self.assertTrue(self.executor.route_ready("p", 11))
        self.executor.defer_route("p", 11, "still no route")
        self.assertFalse(self.executor.route_ready("p", 30))
        self.assertTrue(self.executor.route_ready("p", 31))
        result = self.executor.apply(BehaviorPlan("p", "s", "continue"), motion(), "s", 12)
        self.assertEqual("applied", result.status)
        self.executor.retain_active(())
        self.assertEqual({}, self.executor.route_retry_after)

    def test_rule_can_retry_goal_after_a_continue_but_not_during_activity(self):
        candidate = self.executor.route_provider.build_activity_candidate(motion(),
            {"id": "museum", "kind": "activity", "edge": "b", "stay_seconds": [2, 4]},
            {"id": "exit", "kind": "exit", "edge": "c"})
        state = AgentState("p", current_goal="museum", current_plan=BehaviorPlan("p", "old", "continue"))
        observation = Observation("p", "s", 1, motion())
        engine = AgentDecisionEngine()
        plan = engine.rule_plan(AgentProfile("p"), state, observation, (candidate,))
        self.assertEqual("change_goal", plan.proposed_action)
        self.assertEqual(candidate.arrival_position, plan.arrival_position)
        state.poi_plan_active = True
        self.assertEqual("continue", engine.rule_plan(AgentProfile("p"), state, observation, (candidate,)).proposed_action)


class RuntimeContextTests(unittest.TestCase):
    def setUp(self):
        self.adapter = Adapter()
        routes = {("a", "b"): ["a", "b"], ("a", "c"): ["a", "b", "c"], ("b", "c"): ["b", "c"]}
        self.adapter.find_pedestrian_route.side_effect = lambda a, b, **kwargs: (
            Stage(type=tc.STAGE_WALKING, edges=routes[(a, b)]),)
        self.runtime = SimulationRuntime.__new__(SimulationRuntime)
        self.runtime.route_provider = RouteProvider(Network(), self.adapter)
        self.runtime.plan_executor = PlanExecutor(self.adapter, self.runtime.route_provider)
        self.runtime.adapter = self.adapter
        self.state = AgentState("p", current_goal="museum", activity_plan=["museum", "exit"])
        self.runtime.population = SimpleNamespace(states={"p": self.state}, locked_itinerary_ids=set(),
                                                  profile_for=lambda person_id: AgentProfile(person_id))
        pois = {"museum": {"id": "museum", "edge": "b", "kind": "activity", "stay_seconds": [2, 4]},
                "exit": {"id": "exit", "edge": "c", "kind": "exit"}}
        self.runtime.poi_catalog = SimpleNamespace(pois=pois, available=lambda now, known: [
            target for key, target in pois.items() if key in known])
        self.runtime.activity_planner = SimpleNamespace(initialize=lambda profile, state: None)
        self.runtime.current = SimpleNamespace(time_seconds=1)
        self.runtime.observations = {"p": Observation("p", "s", 1, motion())}

    def test_no_due_agents_means_no_route_work(self):
        self.runtime._refresh_agent_context([])
        self.adapter.find_pedestrian_route.assert_not_called()
        self.runtime._refresh_agent_context(["p"])
        queries = self.adapter.find_pedestrian_route.call_count
        self.runtime._refresh_agent_context([])
        self.assertEqual(queries, self.adapter.find_pedestrian_route.call_count)

    def test_inaccessible_preferred_goal_falls_back(self):
        self.runtime.poi_catalog.pois["unreachable"] = {"id": "unreachable", "edge": "island", "kind": "activity", "stay_seconds": [2, 4]}
        self.state.activity_plan.insert(0, "unreachable")
        self.state.current_goal = "unreachable"
        self.runtime._refresh_agent_context(["p"])
        self.assertEqual("museum", self.state.current_goal)
        self.assertNotIn("unreachable", self.runtime.observations["p"].available_goal_ids)

    def test_waiting_does_not_advance_goal_but_onward_walk_does(self):
        self.state.poi_plan_active = True
        self.state.pending_goal = "exit"
        self.adapter.stages.append(Stage(type=tc.STAGE_WAITING))
        self.runtime.observations["p"] = replace(self.runtime.observations["p"],
                                                own_motion=replace(motion(), stage_type=tc.STAGE_WAITING))
        self.runtime._refresh_agent_context(["p"])
        self.assertEqual("museum", self.state.current_goal)
        self.assertTrue(self.state.poi_plan_active)
        self.adapter.find_pedestrian_route.assert_not_called()
        self.adapter.stages = self.adapter.stages[:1]
        self.runtime.observations["p"] = Observation("p", "s", 1, motion())
        self.runtime._refresh_agent_context(["p"])
        self.assertEqual("exit", self.state.current_goal)
        self.assertEqual(["exit"], self.state.activity_plan)
        self.assertFalse(self.state.poi_plan_active)

    def test_locked_hotspot_itinerary_is_not_replanned(self):
        self.runtime.population.locked_itinerary_ids.add("p")
        self.runtime._refresh_agent_context(["p"])
        self.adapter.find_pedestrian_route.assert_not_called()

    def test_goal_locked_hotspot_gets_route_candidates_without_poi_replanning(self):
        candidate = RouteCandidate(
            "monument", "b", 9, ("a", "b"), 12,
            target_kind="hotspot_route", entry_edge="b", base_cost_seconds=12,
        )
        self.runtime.population.goal_locked_hotspot_ids = {"p": "monument"}
        self.runtime.population.hotspot_target_edges = {"p": "b"}
        self.runtime.population.hotspot_target_positions = {"p": 7.0}
        self.runtime.population.hotspot_initial_entry_edges = {"p": "north_entry"}
        self.runtime.hotspot_catalog = SimpleNamespace(hotspots={
            "monument": {
                "id": "monument", "target_edge": "b", "target_edges": ("b",),
                "route_choice": {"enabled": True},
            }
        })
        self.runtime.hotspot_route_choice = Mock()
        self.runtime.hotspot_route_choice.build_candidates.return_value = (candidate,)
        self.runtime.latest_metrics = {"edges": {}}

        self.runtime._refresh_agent_context(["p"])

        self.assertEqual("monument", self.state.current_goal)
        self.assertEqual((candidate,), self.runtime.decision_candidates["p"])
        self.assertEqual(("monument",), self.runtime.observations["p"].available_goal_ids)
        route_hotspot = self.runtime.hotspot_route_choice.build_candidates.call_args.args[3]
        self.assertEqual(("b", 7.0), (route_hotspot["target_edge"], route_hotspot["target_position"]))
        self.assertEqual("north_entry", self.state.hotspot_entry_edge)



if __name__ == "__main__":
    unittest.main()
