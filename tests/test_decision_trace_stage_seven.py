"""Read-only decision trace, counterfactual contrast and role summary contracts.

QA stubs only: no SUMO TraCI, no external upstream, no learner update, RNG
reseeding or formal archive write. Existing contracts are unchanged; these
tests cover the additive v1 projection blocks.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from crowdsim.domain.crowdsim_models import Observation
from crowdsim.decision.route_provider import RouteCandidate, bind_candidates
from crowdsim.infrastructure.decision_trace import (
    DecisionTraceStore, build_decision_trace, VERSION as TRACE_VERSION)
from crowdsim.infrastructure.counterfactual import build_counterfactual, VERSION as CF_VERSION, MECHANISMS
from crowdsim.infrastructure.role_summary import summarize_traces, VERSION as SUMMARY_VERSION
from crowdsim.modeling_algorithm.individual_model.pipeline import IndividualModelingEngine
from tests.test_observed_feedback_stage_five import fixtures, HOLDS
from tests.test_modeling_binding_stage_one import compiled_model


def _candidate(target_id, edge, edges, cost, snapshot, pid="p"):
    cand = RouteCandidate(target_id, edge, 5.0, edges, cost, target_kind="activity",
                          activity_duration=30.0, next_route_edges=(edge, "z"),
                          next_arrival_position=1.0, next_target_id="z",
                          entry_density_person_per_m2=0.4)
    return bind_candidates((cand,), pid, snapshot)[0]


def _runtime(candidates, *, run_id="r", snapshot=None):
    p, s, o = fixtures()
    if snapshot is not None:
        o = Observation(person_id=o.person_id, snapshot_id=snapshot, time_seconds=o.time_seconds,
                        motion=o.own_motion, objective_density_per_m2=o.objective_density_per_m2,
                        perceived_crowding=o.perceived_crowding, perceived_risk=o.perceived_risk)
    e = IndividualModelingEngine()
    e.configure(compiled_model()["individual_model"]["agent_types"], execution_contract="candidate-decision-v1")
    r = SimpleNamespace(
        decision_engine=e, use_llm=False, run_id=run_id, step_length=1,
        visual_policy=SimpleNamespace(low_speed_threshold_mps=.2),
        population=SimpleNamespace(
            profile_for=lambda pid: p, states={"p": s}, locked_itinerary_ids=set(),
            ledger=SimpleNamespace(unknown_disappearances=set(), explicitly_removed={}, arrived_ids=set())),
        observations={"p": o}, current=SimpleNamespace(persons={"p": o.own_motion}),
        snapshot_id=o.snapshot_id, time_seconds=o.time_seconds,
        decision_candidates={"p": tuple(candidates)},
        adapter=SimpleNamespace(current_person_stage=lambda pid: SimpleNamespace()),
        plan_executor=SimpleNamespace(validate_plan=Mock(), execution_evidence={}),
        decision_traces=DecisionTraceStore(),
    )
    r._post_action_constraints = lambda pid: deepcopy(HOLDS)
    return r, p, s, o


class DecisionTraceProjectionTests(__import__("unittest").TestCase):
    def test_trace_projects_confirmed_stages_with_additive_version_block(self):
        ca = _candidate("a", "b", ("x", "b"), 10, "r:1")
        r, p, s, o = _runtime((ca,))
        trace = build_decision_trace(r, p, s, candidates=(ca,))
        self.assertEqual(TRACE_VERSION, trace["version"])
        self.assertEqual("p", trace["person_id"])
        self.assertEqual("consumer", trace["role_id"])
        self.assertTrue(trace["boundary_ok"])
        self.assertEqual("observed", trace["observation"]["status"])
        # G2 candidate identity/cost card.
        card = trace["candidates"][0]
        self.assertEqual(ca.candidate_id, card["candidate_id"])
        self.assertEqual("change_goal", card["action"])
        self.assertEqual(10, card["estimated_cost_seconds"])
        self.assertEqual(0.4, card["entry_density_person_per_m2"])
        self.assertEqual(("x", "b"), tuple(card["edges"]))
        # Without a frozen affect boundary the game block degrades to unavailable
        # rather than reconstructing PAD/desire from narrative.
        game = trace["game"]
        self.assertEqual("unavailable", game["mode"])
        self.assertEqual([], game["options"])
        self.assertIsNone(game["recommended"])
        # G4 plan/receipt stage is absent rather than LLM-reconstructed.
        self.assertIsNone(trace["plan"])
        self.assertIsNone(trace["receipt"])
        # Learning block reports unattached without fabricating samples.
        self.assertFalse(trace["learning"]["attached"])
        self.assertEqual([], trace["learning"]["events"])

    def test_game_table_projects_once_affect_boundary_is_frozen(self):
        from crowdsim.modeling_algorithm.contracts import PADState
        from crowdsim.modeling_algorithm.individual_model.desire_goal.emotion_driven_desire import (
            EmotionDrivenDesireModel)
        ca = _candidate("a", "b", ("x", "b"), 10, "r:1")
        r, p, s, o = _runtime((ca,))
        desire = EmotionDrivenDesireModel().generate(PADState(), p, s, o)
        r.decision_engine._boundary_contexts["p"] = (
            (o.snapshot_id, o.time_seconds), (PADState(), desire, p, s, o))
        trace = build_decision_trace(r, p, s, candidates=(ca,))
        game = trace["game"]
        self.assertEqual("utility_weighted_probabilistic", game["mode"])
        self.assertIsNotNone(game["rationality"])
        self.assertTrue(game["options"])
        for option in game["options"]:
            self.assertIn("utility", option)
            self.assertIn("probability", option)
            self.assertIn("reason", option)
        self.assertIsNotNone(game["recommended"])
        self.assertIn(game["recommended"]["action"], {x["action"] for x in game["options"]})

    def test_trace_is_read_only_and_does_not_mutate_runtime_state(self):
        ca = _candidate("a", "b", ("x", "b"), 10, "r:1")
        r, p, s, o = _runtime((ca,))
        before_state = deepcopy(s)
        before_obs = deepcopy(o)
        before_counts = (len(r.decision_traces.query(run_id="r")),)
        build_decision_trace(r, p, s, candidates=(ca,))
        self.assertEqual(before_state.__dict__, s.__dict__)
        self.assertEqual(before_obs.__dict__, o.__dict__)
        self.assertEqual(before_counts, (len(r.decision_traces.query(run_id="r")),))

    def test_trace_splits_boundary_and_post_execution_phases(self):
        ca = _candidate("a", "b", ("x", "b"), 10, "r:1")
        r, p, s, o = _runtime((ca,))
        trace = build_decision_trace(r, p, s, candidates=(ca,))
        boundary, post = trace["boundary"], trace["post_execution"]
        self.assertEqual("before_decision", boundary["phase"])
        self.assertEqual("after_decision", post["phase"])
        self.assertEqual(trace["observation"], boundary["observation"])
        self.assertEqual(trace["state"], post["state"])
        self.assertEqual(trace["plan"], post["plan"])
        # Without a frozen boundary context the before-state is not retained;
        # the post-execution state must not be projected as the boundary state.
        self.assertIsNone(boundary["state"])
        self.assertEqual("missing_before_state", boundary["state_status"])

    def test_store_query_filters_by_run_person_snapshot_time(self):
        store = DecisionTraceStore()
        base = {"version": TRACE_VERSION, "run_id": "r1", "person_id": "p",
                "snapshot_id": "r:1", "time_seconds": 1.0}
        store.record({**base, "person_id": "p", "snapshot_id": "r:1", "time_seconds": 1.0})
        store.record({**base, "run_id": "r2", "person_id": "p", "snapshot_id": "r:2", "time_seconds": 2.0})
        store.record({**base, "person_id": "q", "snapshot_id": "r:1", "time_seconds": 1.0})
        self.assertEqual(2, len(store.query(run_id="r1")))
        self.assertEqual(1, len(store.query(run_id="r1", person_id="p")))
        self.assertEqual(2, len(store.query(run_id="r1", snapshot_id="r:1")))
        self.assertEqual(2, len(store.query(run_id="r1", time_seconds=1.0)))
        self.assertEqual(1, len(store.query(run_id="r1", person_id="p", time_seconds=1.0)))
        self.assertIsNone(store.latest(run_id="r1", person_id="missing"))

    def test_pad_archives_real_before_after_across_adjacent_boundaries(self):
        # The production pipeline carries the prior moment's PAD into this moment's
        # smoothing; the first boundary has no prior. Both adjacent records and the
        # same-boundary input factors are archived, not reconstructed by the trace.
        from dataclasses import replace
        from tests.test_observed_feedback_stage_five import fixtures
        p, s, o = fixtures()
        p = replace(p, crowd_role="leisure_visitor")
        e = IndividualModelingEngine()
        e.configure(compiled_model()["individual_model"]["agent_types"], execution_contract="candidate-decision-v1")

        e._prepare_affect(p, s, o)
        first = e._affect_contexts["p"]
        self.assertIsNone(first["before_pad"])
        self.assertEqual("first_boundary", first["continuity"])
        self.assertIsNotNone(first["after_pad"])
        self.assertTrue(first["input_factors"])

        o2 = replace(o, snapshot_id="r:2", time_seconds=2.0)
        e._prepare_affect(p, s, o2)
        second = e._affect_contexts["p"]
        self.assertEqual(first["after_pad"], second["before_pad"])
        self.assertEqual("inertial_update", second["continuity"])
        self.assertIsNotNone(second["after_pad"])
        self.assertTrue(second["input_factors"])


class CounterfactualIsolationTests(__import__("unittest").TestCase):
    def test_whitelisted_mechanism_clones_inputs_and_reports_both_sides(self):
        ca = _candidate("a", "b", ("x", "b"), 10, "r:1")
        r, p, s, _ = _runtime((ca,))
        result = build_counterfactual(r, p, s, "emotion")
        self.assertEqual(CF_VERSION, result["version"])
        self.assertTrue(result["available"])
        self.assertEqual("emotion", result["mechanism"])
        isolation = result["isolation"]
        self.assertTrue(isolation["cloned_profile"])
        self.assertTrue(isolation["cloned_state"])
        self.assertTrue(isolation["cloned_observation"])
        self.assertFalse(isolation["formal_state_touched"])
        self.assertFalse(isolation["formal_learner_touched"])
        self.assertFalse(isolation["sumo_touched"])
        self.assertFalse(isolation["rng_reseeded"])
        self.assertIn("pad", result["baseline"])
        self.assertIn("pad", result["variant"])
        self.assertIn("desire_primary_changed", result["diff"])

    def test_unknown_mechanism_is_unavailable_without_computation(self):
        ca = _candidate("a", "b", ("x", "b"), 10, "r:1")
        r, p, s, _ = _runtime((ca,))
        result = build_counterfactual(r, p, s, "marl")
        self.assertFalse(result["available"])
        self.assertIn("not whitelisted", result["reason"])

    def test_stale_boundary_is_unavailable(self):
        ca = _candidate("a", "b", ("x", "b"), 10, "r:1")
        r, p, s, _ = _runtime((ca,))
        r.snapshot_id = "stale"
        result = build_counterfactual(r, p, s, "desire")
        self.assertFalse(result["available"])
        self.assertIn("stale", result["reason"])


class RoleSummaryTests(__import__("unittest").TestCase):
    def _trace(self, role, person, *, action="continue", participation=None, missing_obs=False):
        observation = {"status": "missing_observation" if missing_obs else "observed",
                       "perceived_risk": 0.3, "perceived_crowding": 0.4}
        return {
            "version": TRACE_VERSION, "run_id": "r1", "person_id": person, "role_id": role,
            "snapshot_id": "r:1", "time_seconds": 1.0,
            "observation": observation,
            "affect": {"status": "evaluated", "desire": {"primary": "safety"}, "pad": None},
            "learning": {"attached": True, "accepted_samples": 1, "update_version": 1, "selections": 1},
            "plan": {"proposed_action": action},
            "participation": participation or [],
        }

    def test_summary_keeps_denominator_missing_and_protected_counts(self):
        traces = [
            self._trace("consumer", "p1", action="change_goal", participation=[
                {"state": "downstream_refined", "action": "change_goal", "target_id": "poi:a",
                 "candidate_id": "cand-1", "target_kind": "route"},
                {"state": "participated_unchanged", "action": "reroute", "target_id": "edge:x",
                 "candidate_id": "cand-2", "target_kind": "route"},
            ]),
            self._trace("consumer", "p2", action="wait", missing_obs=True, participation=[
                {"state": "constraint_blocked", "action": "reroute", "target_id": "edge:y",
                 "candidate_id": "cand-3", "target_kind": "route"},
            ]),
            self._trace("social_visitor", "p3"),
        ]
        summary = summarize_traces(traces, population_persons={"p1": "consumer", "p2": "consumer",
                                                                "p3": "social_visitor", "p4": "commuter"})
        self.assertEqual(SUMMARY_VERSION, summary["version"])
        consumer = summary["roles"]["consumer"]
        self.assertEqual(2, consumer["population"]["persons"])
        self.assertEqual(2, consumer["population"]["with_trace"])
        self.assertEqual(1, consumer["missing"]["no_observation"])
        self.assertEqual(1, consumer["learning"]["downstream_refined"])
        self.assertEqual(1, consumer["learning"]["participated_unchanged"])
        self.assertEqual(1, consumer["learning"]["constraint_blocked"])
        self.assertEqual(1, consumer["learning"]["protected_unchanged"])
        commuter = summary["roles"]["commuter"]
        self.assertEqual(1, commuter["population"]["persons"])
        self.assertEqual(0, commuter["population"]["with_trace"])

    def test_summary_dedupes_repeated_plan_frames_and_keeps_final_arm_state(self):
        first = self._trace("consumer", "p1", action="reroute", participation=[
            {"state": "participated_unchanged", "action": "reroute", "target_id": "edge:x",
             "candidate_id": "cand-2", "target_kind": "route"},
        ])
        second = deepcopy(first)
        second["time_seconds"] = 2.0
        second["participation"] = [
            {"state": "downstream_refined", "action": "reroute", "target_id": "edge:x",
             "candidate_id": "cand-2", "target_kind": "route"},
        ]
        summary = summarize_traces([first, second])
        consumer = summary["roles"]["consumer"]
        # The same persisted plan appearing in two polled frames counts once.
        self.assertEqual(1, consumer["actions"]["reroute"])
        self.assertEqual(1, consumer["route"]["route_plans"])
        # The arm keeps its terminal state; unchanged->refined is not collapsed.
        self.assertEqual(1, consumer["learning"]["downstream_refined"])
        self.assertEqual(0, consumer["learning"]["participated_unchanged"])

    def test_summary_rejects_non_trace_documents(self):
        summary = summarize_traces([{"version": "other", "person_id": "x"}], population_persons={})
        self.assertEqual({}, summary["roles"])


class EndpointRouteTests(__import__("unittest").TestCase):
    def test_read_only_evidence_routes_registered(self):
        from crowdsim.infrastructure.websocket_server import OverlayServer
        sim = SimpleNamespace(
            decision_traces=DecisionTraceStore(), run_id="r1", snapshot_id="r:1", time_seconds=1.0,
            population=SimpleNamespace(states={}, state_for=lambda pid: None),
            profile_for=lambda pid: None)
        server = OverlayServer(sim, "127.0.0.1", 0)
        app = server.create_app()
        paths = {route.resource.canonical for route in app.router.routes()}
        for expected in ("/api/v1/decision-traces", "/api/v1/decision-traces/{person_id}",
                         "/api/v1/role-summary", "/api/v1/counterfactual"):
            self.assertIn(expected, paths)


class ArchiveBackReadTests(__import__("unittest").TestCase):
    def _write_archive(self, root, runtime_id, rows):
        import json
        from pathlib import Path
        directory = Path(root) / runtime_id
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "decision_traces.jsonl").open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_archive_read_filters_by_person_snapshot_time_and_summarizes(self):
        import tempfile
        from crowdsim.infrastructure.decision_trace_archive import load_archive_traces, archive_summary
        runtime_id = "run-" + "a" * 32
        rows = [
            {"version": TRACE_VERSION, "run_id": runtime_id, "person_id": "p", "role_id": "consumer",
             "snapshot_id": "r:1", "time_seconds": 1.0, "observation": {"status": "observed",
             "perceived_risk": 0.2, "perceived_crowding": 0.3}, "affect": {"status": "evaluated"},
             "learning": {"attached": False}, "plan": {"proposed_action": "continue"}, "participation": []},
            {"version": TRACE_VERSION, "run_id": runtime_id, "person_id": "q", "role_id": "consumer",
             "snapshot_id": "r:1", "time_seconds": 1.0, "observation": {"status": "observed",
             "perceived_risk": 0.5, "perceived_crowding": 0.5}, "affect": {"status": "evaluated"},
             "learning": {"attached": False}, "plan": {"proposed_action": "wait"}, "participation": []},
            {"version": TRACE_VERSION, "run_id": runtime_id, "person_id": "p", "role_id": "consumer",
             "snapshot_id": "r:2", "time_seconds": 2.0, "observation": {"status": "observed",
             "perceived_risk": 0.1, "perceived_crowding": 0.2}, "affect": {"status": "evaluated"},
             "learning": {"attached": False}, "plan": {"proposed_action": "continue"}, "participation": []},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            self._write_archive(tmp, runtime_id, rows)
            all_traces, missing = load_archive_traces(runtime_id, root=tmp)
            self.assertEqual([], missing)
            self.assertEqual(3, len(all_traces))
            only_p, _ = load_archive_traces(runtime_id, root=tmp, person_id="p")
            self.assertEqual(2, len(only_p))
            snap, miss = load_archive_traces(runtime_id, root=tmp, snapshot_id="r:2")
            self.assertEqual(1, len(snap))
            time, miss2 = load_archive_traces(runtime_id, root=tmp, time_seconds=2.0)
            self.assertEqual(1, len(time))
            summary = archive_summary(all_traces)
            self.assertEqual(SUMMARY_VERSION, summary["version"])
            self.assertEqual(2, summary["roles"]["consumer"]["population"]["with_trace"])

    def test_missing_archive_returns_explicit_missing_without_fabrication(self):
        import tempfile
        from crowdsim.infrastructure.decision_trace_archive import load_archive_traces
        with tempfile.TemporaryDirectory() as tmp:
            _, missing = load_archive_traces("run-" + "b" * 32, root=tmp)
            self.assertEqual(["runtime_archive_not_found"], missing)
            bad, miss = load_archive_traces("not-a-runtime-id", root=tmp)
            self.assertEqual([], bad)
            self.assertEqual(["no_runtime_run_id"], miss)
            runtime_id = "run-" + "c" * 32
            (Path(tmp) / runtime_id).mkdir(parents=True)
            _, miss2 = load_archive_traces(runtime_id, root=tmp)
            self.assertEqual(["trace_archive_not_found"], miss2)


    def test_archive_rejects_unowned_runtime_and_marks_selection(self):
        from crowdsim.experiments.repository import CrowdSimRepository
        repo = CrowdSimRepository(None)
        current = "run-" + "a" * 32
        previous = "run-" + "b" * 32
        run = repo.create_run("u", "w", "exp-x", "linucb_v1", 0, {})
        repo.update_run("u", "w", run["run_id"],
                        {"runtime_run_id": current, "previous_runtime_run_ids": [previous]})
        # An owned earlier runtime archive passes ownership; archive absence is
        # reported as a missing reason, not fabricated rows.
        result = repo.read_decision_trace_archive("u", "w", run["run_id"], runtime_run_id=previous)
        self.assertEqual(previous, result["selected_runtime_run_id"])
        self.assertEqual(current, result["runtime_run_id"])
        self.assertEqual([previous], result["previous_runtime_run_ids"])
        self.assertFalse(result["mixed_run"])
        # A runtime id that does not belong to this run is rejected outright.
        with self.assertRaises(LookupError) as ctx:
            repo.read_decision_trace_archive("u", "w", run["run_id"], runtime_run_id="run-" + "c" * 32)
        self.assertEqual("RUNTIME_RUN_ID_NOT_OWNED", ctx.exception.args[0])


class CompletedRouteSuccessorTests(__import__("unittest").TestCase):
    def _bounded_runtime(self):
        from tests.test_runtime_learning_stage_five import runtime_stub, settings
        from crowdsim.modeling_algorithm.individual_model.learning.runtime_controller import RoleLearningController
        # runtime_stub builds its own linucb interval controller only to materialize
        # the runtime stub; the bounded controller below is the attached one.
        r, _ = runtime_stub("linucb_v1")
        c = RoleLearningController({"consumer": settings("bounded_adaptation_v1", "completed_route")}, capacity=100)
        c.attach(r, model_fingerprint="sha256:qa")
        return r, c

    def test_legal_route_successors_encoded_without_local_continue_arm(self):
        from crowdsim.decision.route_provider import RouteCandidate, bind_candidates
        r, c = self._bounded_runtime()
        cs = bind_candidates((RouteCandidate("goal", "b", 5, ("a", "b"), 10),), "p", r.snapshot_id)
        r.decision_candidates = {"p": cs}
        choices = c._completed_route_successors(r, "p")
        self.assertEqual(1, len(choices))
        self.assertIn(choices[0].action[0], {"reroute", "change_goal"})
        self.assertTrue(all(ch.action[0] in {"reroute", "change_goal"} for ch in choices))

    def test_no_supplied_successor_leaves_empty_choices_for_unavailable_sample(self):
        r, c = self._bounded_runtime()
        r.decision_candidates = {"p": ()}
        self.assertEqual((), c._completed_route_successors(r, "p"))

    def test_waiting_stage_rejects_route_successor_encoding(self):
        from tests.test_observed_feedback_stage_five import fixtures
        from crowdsim.decision.route_provider import RouteCandidate, bind_candidates
        r, c = self._bounded_runtime()
        cs = bind_candidates((RouteCandidate("goal", "b", 5, ("a", "b"), 10),), "p", r.snapshot_id)
        r.decision_candidates = {"p": cs}
        _, _, o = fixtures(2, stage_type=1)
        r.current.persons = {"p": o.own_motion}
        r.observations = {"p": o}
        self.assertEqual((), c._completed_route_successors(r, "p"))


class GameOptionMirrorTests(__import__("unittest").TestCase):
    def test_evaluate_options_matches_choice_action_space_and_probabilities(self):
        from crowdsim.modeling_algorithm.individual_model.game.bounded_rationality import BoundedRationalGameModel
        from crowdsim.modeling_algorithm.contracts import PADState, DesireState
        p, s, o = fixtures()
        desire = DesireState({k: 0.5 for k in
                              ("safety", "avoid_crowding", "goal_progress", "group_cohesion",
                               "follow_guidance", "rest")}, "safety")
        model = BoundedRationalGameModel()
        table = model.evaluate_options(p, s, o, PADState(), desire, ())
        choice = model.choose(p, s, o, PADState(), desire, ())
        self.assertIn(choice.action, {option["action"] for option in table["options"]})
        probs = [option["probability"] for option in table["options"]]
        self.assertAlmostEqual(1.0, sum(probs), places=6)
        self.assertTrue(all(0.0 <= prob <= 1.0 for prob in probs))


def _frozen_affect_boundary(r):
    from crowdsim.modeling_algorithm.contracts import PADState
    from crowdsim.modeling_algorithm.individual_model.desire_goal.emotion_driven_desire import (
        EmotionDrivenDesireModel)
    p = r.population.profile_for("p")
    s = r.population.states["p"]
    o = r.observations["p"]
    desire = EmotionDrivenDesireModel().generate(PADState(), p, s, o)
    r.decision_engine._boundary_contexts["p"] = (
        (o.snapshot_id, o.time_seconds), (PADState(), desire, p, s, o))


class ProductionChoiceFrozenProbeTests(__import__("unittest").TestCase):
    def test_trace_preserves_frozen_production_choice_and_bias(self):
        ca = _candidate("a", "b", ("x", "b"), 10, "r:1")
        r, p, s, o = _runtime((ca,))
        _frozen_affect_boundary(r)
        r.decision_engine._choice_contexts["p"] = {
            "key": (o.snapshot_id, o.time_seconds),
            "choice": {"action": "wait", "target_id": None, "utility": 0.4,
                       "probabilities": {"wait": 1.0}, "reason": "qa freeze", "candidate_id": None},
            "options": [{"action": "wait", "utility": 0.4, "probability": 1.0, "reason": "qa freeze"}],
            "rationality": 0.7, "selection": "utility_weighted_probabilistic",
            "action_bias": {"continue": 0.2}, "game_capable": True,
        }
        trace = build_decision_trace(r, p, s, candidates=(ca,))
        game = trace["game"]
        self.assertEqual("production_frozen", game["selection_source"])
        self.assertEqual("wait", game["selected"]["action"])
        self.assertEqual({"continue": 0.2}, game["action_bias"])
        self.assertIsNone(game["selected"]["candidate_id"])

    def test_receipt_contract_aliases_requested_action_and_time(self):
        ca = _candidate("a", "b", ("x", "b"), 10, "r:1")
        r, p, s, o = _runtime((ca,))
        r.plan_executor.execution_evidence = {
            "p": {"receipt": {"proposed_action": "reroute", "applied_at": 3.0,
                              "status": "applied", "reason": "ok"}}}
        trace = build_decision_trace(r, p, s, candidates=(ca,))
        receipt = trace["receipt"]
        self.assertEqual("reroute", receipt["requested_action"])
        self.assertEqual(3.0, receipt["time_seconds"])
        self.assertEqual("reroute", receipt["proposed_action"])

    def test_candidate_scores_wire_from_runtime_boundary(self):
        ca = _candidate("a", "b", ("x", "b"), 10, "r:1")
        r, p, s, o = _runtime((ca,))
        r._learning_scores = {"p": {"cand-x": {"score": 0.6, "probability": 0.7}}}
        trace = build_decision_trace(r, p, s, candidates=(ca,))
        self.assertEqual(0.6, trace["candidate_scores"]["cand-x"]["score"])

    def test_replay_buffer_reports_sample_ids_without_feature_payload(self):
        ca = _candidate("a", "b", ("x", "b"), 10, "r:1")
        r, p, s, o = _runtime((ca,))
        trace = build_decision_trace(r, p, s, candidates=(ca,))
        self.assertFalse(trace["learning"]["attached"])
        self.assertNotIn("replay", trace["learning"])


class RoleSummaryDedupProbeTests(__import__("unittest").TestCase):
    def _frame(self, person, time, interval_id, exposure, participation):
        return {
            "version": TRACE_VERSION, "run_id": "r1", "person_id": person, "role_id": "consumer",
            "snapshot_id": f"r:{time}", "time_seconds": float(time),
            "observation": {"status": "observed", "perceived_risk": 0.3, "perceived_crowding": 0.4},
            "affect": {"status": "evaluated", "desire": {"primary": "safety"}, "pad": None},
            "learning": {"attached": True, "accepted_samples": 1, "update_version": 1, "selections": 1,
                          "events": [{"kind": "interval", "interval_id": interval_id, "trainable": True,
                                      "risk_exposure_seconds": exposure}]},
            "plan": {"proposed_action": "change_goal"},
            "participation": participation,
        }

    def test_counters_take_latest_per_person_and_events_dedup_by_identity(self):
        participation = [{"state": "downstream_refined", "target_kind": "route"}]
        traces = [
            self._frame("p1", 1, "i1", 2.0, participation),
            self._frame("p1", 2, "i1", 2.0, participation),
        ]
        summary = summarize_traces(traces)
        consumer = summary["roles"]["consumer"]
        self.assertEqual(1, consumer["learning"]["accepted_samples"])
        self.assertEqual(1, consumer["learning"]["updates"])
        self.assertEqual(1, consumer["learning"]["selections"])
        self.assertEqual(1, consumer["route"]["completed_route_evidence"])
        self.assertEqual(1, consumer["learning"]["downstream_refined"])
        self.assertEqual(2.0, consumer["risk"]["risk_exposure_seconds"])


class CounterfactualBiasGuardProbeTests(__import__("unittest").TestCase):
    def test_mirror_without_replay_bias_is_unavailable_when_bias_present(self):
        ca = _candidate("a", "b", ("x", "b"), 10, "r:1")
        r, p, s, _ = _runtime((ca,))
        baseline_trace = {"game": {"selection_source": "mirror", "options": [],
                                   "action_bias": {"wait": 0.3}}}
        result = build_counterfactual(r, p, s, "emotion", baseline_trace=baseline_trace)
        self.assertFalse(result["available"])
        self.assertIn("bias", result["reason"])

    def test_stale_baseline_trace_snapshot_is_unavailable(self):
        ca = _candidate("a", "b", ("x", "b"), 10, "r:1")
        r, p, s, _ = _runtime((ca,))
        stale = {"snapshot_id": "other-snapshot", "time_seconds": 99.0, "game": {}}
        result = build_counterfactual(r, p, s, "desire", baseline_trace=stale)
        self.assertFalse(result["available"])
        self.assertIn("stale", result["reason"])


class LearningBindingAttributionTests(__import__("unittest").TestCase):
    """Per-person learning counters come from a bound personal learner only.

    Controller-global accepted/update/selection totals must not leak onto a
    person whose role is unconfigured for learning or whose personal learner is
    not instantiated.
    """

    def _controller_runtime(self):
        from tests.test_runtime_learning_stage_five import runtime_stub
        r, c = runtime_stub("linucb_v1", 100)
        r.learning_controller = c
        # Mimic a long run's controller-global totals; these are read-only and
        # must not be projected onto an unbound person.
        c.accepted_samples = 35940
        c.actual_updates = 35940
        c.selection_count = 6872
        return r, c

    def test_unconfigured_role_does_not_inherit_global_learning_counters(self):
        from crowdsim.domain.crowdsim_models import AgentProfile, AgentState
        r, c = self._controller_runtime()
        q = AgentProfile("q", crowd_role="commuter", free_walking_speed=2)
        sq = AgentState("q", stress=.2, fatigue=.3)
        _, _, o = fixtures()
        r.population.profile_for = lambda pid: q
        r.population.states = {"q": sq}
        r.observations = {"q": o}
        r.current = SimpleNamespace(persons={"q": o.own_motion})
        learning = build_decision_trace(r, q, sq)["learning"]
        self.assertFalse(learning["attached"])
        self.assertIsNone(learning["method_id"])
        self.assertEqual(0, learning["accepted_samples"])
        self.assertEqual(0, learning["update_version"])
        self.assertEqual(0, learning["selections"])
        self.assertIsNone(learning["binding"])
        self.assertEqual(35940, c.accepted_samples)  # global counters untouched

    def test_configured_role_without_personal_learner_is_not_attached(self):
        r, c = self._controller_runtime()
        p, s, _ = fixtures()
        learning = build_decision_trace(r, p, s)["learning"]
        self.assertFalse(learning["attached"])
        self.assertEqual("linucb_v1", learning["method_id"])
        self.assertEqual(0, learning["accepted_samples"])
        self.assertEqual(0, learning["update_version"])
        self.assertEqual(0, learning["selections"])

    def test_bound_learner_reports_personal_counts_not_global_selection_total(self):
        r, c = self._controller_runtime()
        p, s, _ = fixtures()
        c._person(r, "p")
        c._emit("selection", person_id="p", snapshot_id=r.snapshot_id,
                time_seconds=r.time_seconds, baseline={}, selected={},
                scores=[], policy="baseline_family_preserved")
        learning = build_decision_trace(r, p, s)["learning"]
        self.assertTrue(learning["attached"])
        self.assertEqual(0, learning["accepted_samples"])  # personal diagnostics, not global 35940
        self.assertEqual(0, learning["update_version"])
        self.assertEqual(1, learning["selections"])        # own event count, not global 6872
        self.assertIsNotNone(learning["binding"]["binding_fingerprint"])
