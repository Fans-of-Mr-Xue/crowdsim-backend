"""Controller causal selection: learned policy acts on a real legal plan and feeds back.

These are isolated QA bindings against the original Runtime contract. They do not
replace production defaults and do not run a natural SUMO population; natural-run
evidence is collected separately in unified end-to-end validation.
"""
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import Mock

from crowdsim.domain.crowdsim_models import BehaviorPlan, PlanExecutionResult
from tests.test_runtime_learning_stage_five import settings, runtime_stub, begin, next_step, local
from tests.test_observed_feedback_stage_five import fixtures, HOLDS


def trained_stub(method='tabular_q_v1'):
    r, c = runtime_stub(method)
    begin(r, c)
    next_step(r, 2)
    c.advance(r, timeline_end=False)
    c.refine(r, local(r))
    return r, c


def plans_of(r, c, plan=None):
    c._person(r, plan.person_id if plan else 'p')
    return c._plans(r, plan or local(r))


class LearningActionSelectionTests(unittest.TestCase):
    def test_local_family_offers_continue_slow_down_wait_and_keeps_itinerary_clear(self):
        r, c = trained_stub()
        valid = plans_of(r, c)
        actions = sorted(e.action[0] for _, e in valid)
        self.assertEqual(['continue', 'slow_down', 'wait'], actions)
        for plan, _ in valid:
            self.assertIsNone(plan.target_id)
            self.assertEqual((), plan.route_edges)
            self.assertIsNone(plan.candidate_id)
            self.assertEqual((), plan.next_route_edges)
            self.assertIsNone(plan.next_target_id)
        wait = next(p for p, e in valid if e.action[0] == 'wait')
        self.assertGreater(wait.wait_until, r.time_seconds)
        slow = next(p for p, e in valid if e.action[0] == 'slow_down')
        self.assertIsNotNone(slow.speed_limit)
        self.assertGreaterEqual(slow.speed_limit, 0.2)

    def test_learned_policy_selects_wait_and_selection_is_audited(self):
        r, c = trained_stub()
        learner = c.learners['p']
        valid = plans_of(r, c)
        wait_key = next(e.action for _, e in valid if e.action[0] == 'wait')
        score = {e.action: (1.0 if e.action == wait_key else 0.0) for _, e in valid}
        learner.score = Mock(return_value=score)
        c.selection_count = 0
        selected = c.refine(r, local(r))
        self.assertEqual('wait', selected.proposed_action)
        self.assertEqual(1, c.selection_count)
        selection_rows = [json.loads(x) for x in c.batch() if json.loads(x)['kind'] == 'selection']
        row = selection_rows[-1]
        self.assertEqual('wait', row['selected']['proposed_action'])
        self.assertEqual('continue', row['baseline']['proposed_action'])
        self.assertEqual('baseline_family_preserved', row['policy'])
        learner.score.assert_called()

    def test_q_value_argmax_drives_next_decision_after_observed_feedback(self):
        r, c = trained_stub('tabular_q_v1')
        learner = c.learners['p']
        valid = plans_of(r, c)
        wait_key = next(e.action for _, e in valid if e.action[0] == 'wait')
        state = valid[0][1].state
        # Learned table is the only policy source; baseline rule plan stays continue.
        learner.values[(state, ('continue',))] = -1.0
        learner.values[(state, wait_key)] = 1.0
        selected = c.refine(r, local(r))
        self.assertEqual('wait', selected.proposed_action)
        # Reverse the learned preference: the next decision follows the updated table.
        learner.values[(state, ('continue',))] = 2.0
        learner.values[(state, wait_key)] = -1.0
        again = c.refine(r, local(r))
        self.assertEqual('continue', again.proposed_action)

    def test_goal_route_target_drops_local_alternatives(self):
        s = settings('linucb_v1')
        s['learning_config']['targets'] = ['goal_route']
        from crowdsim.modeling_algorithm.individual_model.learning.runtime_controller import RoleLearningController
        r, _ = runtime_stub()
        c = RoleLearningController({'consumer': s}, capacity=100)
        c.attach(r, model_fingerprint='sha256:qa')
        valid = plans_of(r, c)
        self.assertEqual(0, len(valid))
        self.assertEqual(local(r), c.refine(r, local(r)))
        self.assertEqual(0, c.selection_count)

    def test_activity_policy_flow_locked_poi_dwelling_keep_baseline(self):
        r, c = trained_stub()
        scenario_holds = [
            ('activity_hold', {**deepcopy(HOLDS), 'activity_hold': True}),
            ('policy_wait', {**deepcopy(HOLDS), 'policy_wait': True}),
            ('flow_hold', {**deepcopy(HOLDS), 'flow_hold': ['inflow']}),
        ]
        for label, holds in scenario_holds:
            with self.subTest(label=label):
                r._post_action_constraints = lambda pid, h=holds: deepcopy(h)
                self.assertEqual(1, len(plans_of(r, c)))
                self.assertEqual(local(r), c.refine(r, local(r)))
        r._post_action_constraints = lambda pid: deepcopy(HOLDS)
        r.population.locked_itinerary_ids.add('p')
        self.assertEqual(1, len(plans_of(r, c)))
        self.assertEqual(local(r), c.refine(r, local(r)))
        r.population.locked_itinerary_ids.discard('p')
        state = r.population.states['p']
        state.poi_plan_active = True
        self.assertEqual(1, len(plans_of(r, c)))
        state.poi_plan_active = False
        state.activity_state = 'hotspot_dwelling'
        self.assertEqual(1, len(plans_of(r, c)))
        state.activity_state = 'walking'

    def test_selected_local_plan_carries_no_goal_or_onward_payload(self):
        r, c = trained_stub()
        learner = c.learners['p']
        valid = plans_of(r, c)
        slow_key = next(e.action for _, e in valid if e.action[0] == 'slow_down')
        learner.score = Mock(return_value={e.action: (1.0 if e.action == slow_key else 0.0) for _, e in valid})
        selected = c.refine(r, local(r))
        self.assertEqual('slow_down', selected.proposed_action)
        self.assertIsNone(selected.target_id)
        self.assertEqual((), selected.route_edges)
        self.assertIsNone(selected.candidate_id)
        self.assertIsNone(selected.activity_duration)
        self.assertEqual((), selected.next_route_edges)
        self.assertIsNone(selected.next_arrival_position)
        self.assertIsNone(selected.next_target_id)
        self.assertFalse(selected.preserve_future_stages)

    def test_route_refinement_keeps_same_goal_and_picks_legal_entrance(self):
        from crowdsim.decision.route_provider import RouteCandidate, bind_candidates
        r, c = trained_stub('tabular_q_v1')
        cs = bind_candidates((
            RouteCandidate('goal', 'b', 5, ('a', 'b'), 10, entry_edge='e0'),
            RouteCandidate('goal', 'b', 5, ('x', 'b'), 8, entry_edge='e1'),
        ), 'p', r.snapshot_id)
        r.decision_candidates = {'p': cs}
        base = replace(local(r), proposed_action='reroute', target_id='goal',
            route_edges=cs[0].edges, arrival_position=5, candidate_id=cs[0].candidate_id)
        valid = plans_of(r, c, base)
        self.assertEqual(2, len(valid))
        other = next(p for p, e in valid if e.action != valid[0][1].action)
        other_key = next(e.action for p, e in valid if p.candidate_id == cs[1].candidate_id)
        learner = c.learners['p']
        learner.score = Mock(return_value={e.action: (1.0 if e.action == other_key else 0.0) for _, e in valid})
        c.selection_count = 0
        selected = c.refine(r, base)
        self.assertEqual(cs[1].candidate_id, selected.candidate_id)
        self.assertEqual(('x', 'b'), selected.route_edges)
        self.assertEqual('goal', selected.target_id)
        self.assertEqual(5, selected.arrival_position)
        self.assertEqual(1, c.selection_count)
        # No other goal and no onward stage payload survive the refinement.
        self.assertIsNone(selected.next_target_id)
        self.assertEqual((), selected.next_route_edges)
        self.assertNotEqual(valid[0][0].candidate_id, selected.candidate_id)
        self.assertIsNotNone(other)

    def test_unconfigured_role_never_gets_local_alternative(self):
        r, c = runtime_stub()
        r.population.profile_for = lambda pid: replace(fixtures()[0], crowd_role='commuter')
        self.assertEqual(local(r), c.refine(r, local(r)))
        self.assertNotIn('p', c.learners)


if __name__ == '__main__':
    unittest.main()
