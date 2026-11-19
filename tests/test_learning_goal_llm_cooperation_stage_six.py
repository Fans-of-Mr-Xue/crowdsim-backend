"""Goal-arm legal selection, learning/LLM cooperation and callable-gate QA.

QA bindings only: memory stubs, mocked executor/request. They do not replace
production defaults, open a real external upstream, or write formal archives.
"""
import asyncio
import json
import os
import time
import unittest
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from crowdsim.domain.crowdsim_models import BehaviorPlan, PlanExecutionResult
from crowdsim.decision.route_provider import RouteCandidate, bind_candidates
from crowdsim.modeling_algorithm.individual_model.learning.runtime_controller import RoleLearningController
from crowdsim.modeling_algorithm.individual_model.learning.observed_feedback import ACTION_VERSION, FeatureEncoder
from crowdsim.modeling_algorithm.individual_model.learning.reward_bridge import RULE_ID, IntervalRewardRule
from crowdsim.modeling_algorithm.individual_model.decision.llm_driven import LLMDrivenDecisionModel
from crowdsim.modeling_algorithm.individual_model.pipeline import IndividualModelingEngine
from tests.test_learning_configuration_stage_five import config
from tests.test_observed_feedback_stage_five import fixtures, stage, HOLDS
from tests.test_modeling_binding_stage_one import compiled_model


def settings(mode='candidate_scoring'):
    enc = dict(columns=['bias', 'perceived_risk'], scales={'bias': 1, 'perceived_risk': 1},
               state_bins={'perceived_risk': [.1, .5]}, emotion_participates=False)
    encoder = FeatureEncoder(columns=tuple(enc['columns']), scales=enc['scales'],
        state_bins={k: tuple(v) for k, v in enc['state_bins'].items()}, emotion_participates=False)
    reward = IntervalRewardRule(weights={'risk_exposure': 1, 'switch_cost': 10}, scales={'risk_exposure': 1, 'switch_cost': 1})
    c = config('linucb_v1')
    c['parameters'].update(minimum_samples=1, update_every=1, update_budget=1)
    c['feature_schema_version'] = encoder.fingerprint
    c['action_schema_version'] = ACTION_VERSION
    c['feedback'] = {'rule_id': RULE_ID, 'version': reward.fingerprint, 'weights': reward.spec['weights']}
    return dict(learning_config=c, encoding=enc, reward_scales=reward.spec['scales'],
        interval_mode='decision_interval', integration_mode=mode,
        storage_capacity=100, bootstrap_on_truncation=False)


def activity_candidate(target_id, edge, edges, cost, *, next_target='z', snapshot='r:1', pid='p'):
    cand = RouteCandidate(target_id, edge, 5.0, edges, cost, target_kind='activity',
        activity_duration=30.0, next_route_edges=(edge, next_target), next_arrival_position=1.0,
        next_target_id=next_target)
    return bind_candidates((cand,), pid, snapshot)[0]


def stub(role_settings, *, candidates=(), llm_decision=False, locked=(), attach=True):
    p, s, o = fixtures()
    e = IndividualModelingEngine()
    e.configure(compiled_model()['individual_model']['agent_types'], execution_contract='candidate-decision-v1')
    if llm_decision:
        e.assemblies['consumer'].decision = LLMDrivenDecisionModel()
    r = SimpleNamespace(
        decision_engine=e, use_llm=llm_decision, run_id='r', step_length=1,
        visual_policy=SimpleNamespace(low_speed_threshold_mps=.2),
        population=SimpleNamespace(
            profile_for=lambda pid: p, states={'p': s},
            locked_itinerary_ids=set(locked),
            ledger=SimpleNamespace(unknown_disappearances=set(), explicitly_removed={}, arrived_ids=set())),
        observations={'p': o}, current=SimpleNamespace(persons={'p': o.own_motion}),
        snapshot_id=o.snapshot_id, time_seconds=o.time_seconds,
        decision_candidates={'p': tuple(candidates)},
        adapter=SimpleNamespace(current_person_stage=lambda pid: SimpleNamespace(**stage().as_dict())),
        plan_executor=SimpleNamespace(validate_plan=Mock()), last_error='')
    r._post_action_constraints = lambda pid: deepcopy(HOLDS)
    c = RoleLearningController({'consumer': role_settings}, capacity=100)
    if attach:
        c.attach(r, model_fingerprint='sha256:qa')
    return r, c


def goal_plan(candidate, *, source='configured_rule', snapshot='r:1', now=1):
    return replace(LLMDrivenDecisionModel._make_plan(
        SimpleNamespace(person_id='p', free_walking_speed=2, mobility=1),
        type('O', (), {'snapshot_id': snapshot, 'time_seconds': now})(),
        'change_goal', candidate, reason='qa baseline', confidence=.5, source=source),
        scheduling_source=source)


class GoalArmSelectionTests(unittest.TestCase):
    def test_scoring_mode_selects_another_nonlocked_legal_activity_goal(self):
        ca = activity_candidate('a', 'b', ('x', 'b'), 10, snapshot='r:1')
        cb = activity_candidate('b', 'b2', ('y', 'b2'), 8, snapshot='r:1')
        r, c = stub(settings(), candidates=(ca, cb))
        learner = c._person(r, 'p')
        learner.updates = 1
        learner.accepted = 1
        baseline = goal_plan(ca)
        # Force the learner to prefer the second supplied legal goal candidate.
        chosen_key = None
        real_choose = learner.choose
        def choose(state, choices, *, token):
            nonlocal chosen_key
            chosen_key = next(x.key for x in choices if x.key[1] == 'b')
            return chosen_key
        learner.choose = choose
        selected = c.refine(r, baseline)
        self.assertEqual('change_goal', selected.proposed_action)
        self.assertEqual('b', selected.target_id)
        self.assertEqual(cb.candidate_id, selected.candidate_id)
        self.assertEqual(('y', 'b2'), tuple(selected.route_edges))
        rows = [json.loads(x) for x in c.batch() if json.loads(x)['kind'] == 'selection']
        self.assertEqual('candidate_scoring', rows[-1]['policy'])
        self.assertEqual('b', rows[-1]['selected']['target_id'])

    def test_locked_itinerary_keeps_baseline_goal_without_alternatives(self):
        ca = activity_candidate('a', 'b', ('x', 'b'), 10)
        cb = activity_candidate('b', 'b2', ('y', 'b2'), 8)
        r, c = stub(settings(), candidates=(ca, cb), locked=('p',))
        learner = c._person(r, 'p')
        learner.updates = 1
        selected = c.refine(r, goal_plan(ca))
        self.assertEqual('a', selected.target_id)
        self.assertEqual(ca.candidate_id, selected.candidate_id)
        self.assertFalse(any(json.loads(x)['kind'] == 'selection' for x in c.batch()))

    def test_candidate_scores_expose_legal_goal_choices_without_inventing_candidates(self):
        ca = activity_candidate('a', 'b', ('x', 'b'), 10)
        cb = activity_candidate('b', 'b2', ('y', 'b2'), 8)
        r, c = stub(settings(), candidates=(ca, cb))
        learner = c._person(r, 'p')
        learner.updates = 1
        scores = c.candidate_scores(r, goal_plan(ca))
        self.assertEqual({ca.candidate_id, cb.candidate_id}, set(scores))
        for entry in scores.values():
            self.assertIn('action', entry)
            self.assertIn('score', entry)
        # An unknown target cannot appear in the supplied legal set.
        self.assertNotIn('unknown-target', scores)


class LearningLlmCooperationTests(unittest.TestCase):
    def test_llm_refine_mode_attaches_to_llm_assembly_and_tracks_llm_source_plan(self):
        r, c = stub(settings('llm_baseline_refine'), llm_decision=True)
        decision = r.decision_engine.assemblies['consumer'].decision
        self.assertIsInstance(decision, LLMDrivenDecisionModel)
        plan = BehaviorPlan('p', r.observations['p'].snapshot_id, 'continue',
            decided_at=r.time_seconds, scheduling_source='llm_request')
        prepared = c.prepare(r, plan)
        self.assertIsNotNone(prepared)
        c.accept(r, prepared, PlanExecutionResult('p', 'continue', 'continue', 'applied', 1))
        self.assertIn('p', c.contexts)
        # The applied LLM-baseline plan is observed; reward/next-decision chain is the
        # normal controller accept -> interval -> update path.
        self.assertEqual(1, len(c.journal.pending))

    def test_rule_only_mode_still_rejects_llm_assembly(self):
        r, _ = stub(settings('baseline_route_refinement'), llm_decision=True, attach=False)
        self.assertIsNotNone(r.decision_engine.assemblies['consumer'].decision)
        with self.assertRaises(ValueError):
            RoleLearningController({'consumer': settings('baseline_route_refinement')}, capacity=100).attach(
                r, model_fingerprint='sha256:qa')


class CallableGateTests(unittest.TestCase):
    def test_default_unconfigured_is_not_callable_and_does_not_leak_key(self):
        old = os.environ.get('CROWDSIM_MODEL_API_KEY')
        os.environ.pop('CROWDSIM_MODEL_API_KEY', None)
        try:
            model = LLMDrivenDecisionModel()
            self.assertFalse(model.online_ready)
            self.assertFalse(model.callable)
            self.assertEqual('unconfigured', model.online_mode)
            diag = model.diagnostics()
            self.assertNotIn('api_key', diag)
            self.assertNotIn('CROWDSIM_MODEL_API_KEY', diag)
        finally:
            if old is not None:
                os.environ['CROWDSIM_MODEL_API_KEY'] = old

    def test_explicit_local_qa_configuration_is_callable_without_key(self):
        model = LLMDrivenDecisionModel()
        model.qa_local = True
        self.assertTrue(model.online_ready)
        self.assertTrue(model.callable)
        self.assertEqual('local_qa', model.online_mode)

    def test_non_default_url_without_key_is_not_configured_external(self):
        model = LLMDrivenDecisionModel()
        model.base_url = 'https://example.invalid/v1'
        model.api_key = ''
        self.assertFalse(model.online_ready)
        self.assertFalse(model.callable)
        self.assertEqual('unconfigured', model.online_mode)

    def test_qa_local_rejects_non_loopback_host_and_non_http_scheme(self):
        for url in ['https://example.invalid/v1', 'http://10.0.0.5:8800/v1', 'ftp://127.0.0.1/v1']:
            model = LLMDrivenDecisionModel()
            model.qa_local = True
            model.base_url = url
            self.assertFalse(model._qa_local_valid, url)
            self.assertFalse(model.callable, url)
            self.assertEqual('unconfigured', model.online_mode, url)
        for url in ['http://localhost:8800/v1', 'http://127.0.0.1:8800/v1', 'http://[::1]:8800/v1']:
            self.assertTrue(LLMDrivenDecisionModel._local_endpoint_allowed(url), url)

    def test_configured_but_circuit_broken_is_not_callable(self):
        model = LLMDrivenDecisionModel()
        model.qa_local = True
        model._unavailable_until = time.monotonic() + 30
        self.assertTrue(model.online_ready)
        self.assertFalse(model.callable)

    def test_configured_key_reports_external_mode_without_value_in_diagnostics(self):
        model = LLMDrivenDecisionModel()
        model.api_key = 'qa-secret'
        self.assertTrue(model.callable)
        self.assertEqual('external', model.online_mode)
        dumped = json.dumps(model.diagnostics(), ensure_ascii=False)
        self.assertNotIn('qa-secret', dumped)

    def test_unconfigured_decide_falls_back_to_rule_without_request(self):
        model = LLMDrivenDecisionModel()
        called = []
        async def request(_messages):
            called.append(1)
            return {'action': 'continue'}
        model._request = request
        p, s, o = fixtures()
        desire = __import__('crowdsim.modeling_algorithm.individual_model.desire_goal.emotion_driven_desire',
            fromlist=['EmotionDrivenDesireModel']).EmotionDrivenDesireModel().generate(
            __import__('crowdsim.modeling_algorithm.contracts', fromlist=['PADState']).PADState(), p, s, o)
        choice = __import__('crowdsim.modeling_algorithm.individual_model.game.bounded_rationality',
            fromlist=['BoundedRationalGameModel']).BoundedRationalGameModel().choose(
            p, s, o, __import__('crowdsim.modeling_algorithm.contracts', fromlist=['PADState']).PADState(), desire, ())
        plan = asyncio.run(model.decide(p, s, o, (), __import__('crowdsim.modeling_algorithm.contracts',
            fromlist=['PADState']).PADState(), desire, choice))
        self.assertEqual('bounded_rational_fallback', plan.source)
        self.assertEqual([], called)


def _decision_inputs():
    from crowdsim.modeling_algorithm.contracts import PADState
    from crowdsim.modeling_algorithm.individual_model.desire_goal.emotion_driven_desire import EmotionDrivenDesireModel
    from crowdsim.modeling_algorithm.individual_model.game.bounded_rationality import BoundedRationalGameModel
    p, s, o = fixtures()
    pad = PADState()
    desire = EmotionDrivenDesireModel().generate(pad, p, s, o)
    choice = BoundedRationalGameModel().choose(p, s, o, pad, desire, ())
    return p, s, o, pad, desire, choice


class AdvisoryScoreInjectionTests(unittest.TestCase):
    def test_advisory_scores_reach_llm_messages_without_replacing_candidate_contract(self):
        ca = activity_candidate('a', 'b', ('x', 'b'), 10)
        model = LLMDrivenDecisionModel()
        model.qa_local = True
        model.candidate_contract = True
        captured = []

        async def request(messages):
            captured.append(messages)
            return {'action': 'change_goal', 'candidate_id': ca.candidate_id,
                    'reason': 'qa', 'confidence': .8}

        model._request = request
        p, s, o, pad, desire, choice = _decision_inputs()
        scores = {ca.candidate_id: {'action': ['change_goal', 'a'], 'score': 0.72}}
        plan = asyncio.run(model.decide(p, s, o, (ca,), pad, desire, choice, candidate_scores=scores))
        self.assertEqual(ca.candidate_id, plan.candidate_id)
        context = json.loads(captured[0][1]['content'])
        advisory = {item['candidate_id']: item for item in context['advisory_learning_scores']}
        self.assertEqual(0.72, advisory[ca.candidate_id]['score'])
        self.assertEqual(['change_goal', 'a'], advisory[ca.candidate_id]['action'])
        self.assertEqual(1, model.diagnostics()['advisory_scores'])

    def test_malformed_advisory_scores_are_dropped_and_valid_one_kept(self):
        model = LLMDrivenDecisionModel()
        cleaned = model._validate_advisory_scores({
            'good': {'action': ['continue'], 'score': 0.5},
            'bad-inf': {'action': ['continue'], 'score': float('inf')},
            'bad-action': {'action': 'continue', 'score': 0.5},
            'bad-score': {'action': ['continue'], 'score': 'high'},
            123: {'action': ['continue'], 'score': 0.5},
        })
        self.assertEqual({'good'}, set(cleaned))
        self.assertEqual(0.5, cleaned['good']['score'])

    def test_stale_advisory_candidate_id_is_dropped_against_boundary_set(self):
        ca = activity_candidate('a', 'b', ('x', 'b'), 10)
        model = LLMDrivenDecisionModel()
        model.qa_local = True
        model.candidate_contract = True
        captured = []

        async def request(messages):
            captured.append(messages)
            return {'action': 'change_goal', 'candidate_id': ca.candidate_id, 'reason': 'qa', 'confidence': .8}

        model._request = request
        p, s, o, pad, desire, choice = _decision_inputs()
        stale_scores = {
            ca.candidate_id: {'action': ['change_goal', 'a'], 'score': 0.4},
            'stale-id': {'action': ['change_goal', 'z'], 'score': 0.9},
        }
        asyncio.run(model.decide(p, s, o, (ca,), pad, desire, choice, candidate_scores=stale_scores))
        context = json.loads(captured[0][1]['content'])
        self.assertEqual({ca.candidate_id}, {item['candidate_id'] for item in context['advisory_learning_scores']})

    def test_llm_response_with_unknown_candidate_still_rejected(self):
        ca = activity_candidate('a', 'b', ('x', 'b'), 10)
        p, s, o, pad, desire, choice = _decision_inputs()
        # Parse-time contract still rejects an identity outside the supplied boundary.
        with self.assertRaises(ValueError):
            LLMDrivenDecisionModel._parse_plan(
                {'action': 'change_goal', 'candidate_id': 'unknown-candidate'},
                p, o, (ca,), candidate_contract=True)
        # At the decide boundary a rejected payload degrades to the rule/game fallback
        # instead of binding the unknown candidate.
        model = LLMDrivenDecisionModel()
        model.qa_local = True
        model.candidate_contract = True

        async def request(_messages):
            return {'action': 'change_goal', 'candidate_id': 'unknown-candidate'}

        model._request = request
        plan = asyncio.run(model.decide(p, s, o, (ca,), pad, desire, choice,
            candidate_scores={ca.candidate_id: {'action': ['change_goal', 'a'], 'score': 0.3}}))
        self.assertEqual('bounded_rational_fallback', plan.source)
        self.assertIsNone(plan.candidate_id)

    def test_boundary_scores_empty_for_local_baseline_and_seeded_for_route_plan(self):
        ca = activity_candidate('a', 'b', ('x', 'b'), 10)
        cb = activity_candidate('b', 'b2', ('y', 'b2'), 8)
        r, c = stub(settings(), candidates=(ca, cb))
        learner = c._person(r, 'p')
        learner.updates = 1
        learner.accepted = 1
        # Local/absent baseline: no advisory route scores.
        r.population.states['p'].current_plan = BehaviorPlan('p', 'r:1', 'continue', decided_at=1)
        self.assertEqual({}, c.boundary_scores(r, 'p'))
        # Previously applied route goal plan seeds the legal candidate set.
        r.population.states['p'].current_plan = goal_plan(ca)
        scores = c.boundary_scores(r, 'p')
        self.assertEqual({ca.candidate_id, cb.candidate_id}, set(scores))
        for entry in scores.values():
            self.assertIn('action', entry)
            self.assertTrue(entry['score'] is None or isinstance(entry['score'], float))


if __name__ == '__main__':
    unittest.main()
