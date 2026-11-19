"""Confirmed role settings: precise version binding and automatic Runtime path."""
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import Mock,patch
from crowdsim.modeling_algorithm.individual_model.learning.configured_runtime import (
    CONFIGURED_LEARNING_CONTRACT,prepare_role_learning_settings,validate_configured_roles)
from crowdsim.modeling_algorithm.individual_model.learning.observed_feedback import FEATURE_VERSION
from crowdsim.modeling_algorithm.individual_model.module_assembly import validate_assembly
from crowdsim.modeling_algorithm.individual_model.pipeline import IndividualModelingEngine
from crowdsim.modeling_algorithm.runtime_binding import build_runtime_binding
from crowdsim.modeling_algorithm.compiler import compile_artificial_society
from crowdsim.domain.artificial_society_spec import ArtificialSocietySpec
from crowdsim.core.simulation_runtime import SimulationRuntime
from tests.test_runtime_learning_stage_five import settings,runtime_stub
from tests.test_modeling_binding_stage_one import compiled_model
from tests.test_modeling_requirements_compiler import requirement_payload,default_modeling_requirements,confirm_modeling,environment_candidates


def confirmed_settings(method='linucb_v1'):
    raw=settings(method,'completed_route' if method=='bounded_adaptation_v1' else 'decision_interval')
    config=raw.pop('learning_config');config['feature_schema_version']=FEATURE_VERSION;config['feedback']['version']='1'
    raw['audit_capacity']=4096
    return config,raw


def agents():
    rows=compiled_model()['individual_model']['agent_types']
    for a in rows:
        if a['capabilities']['learning']:
            method='tabular_q_v1' if a['id']=='leisure_visitor' else 'linucb_v1' if a['id']=='social_visitor' else 'bounded_adaptation_v1'
            a['learning_config'],a['learning_runtime']=confirmed_settings(method)
            next(x for x in a['architecture'] if x['id']=='learning')['method']={'tabular_q_v1':'表格 Q-learning','linucb_v1':'上下文 bandit（LinUCB）','bounded_adaptation_v1':'有界参数自适应'}[method]
    return rows


def model():
    r=requirement_payload();r['population']['distributions']['crowd_role']=[{'code':a['id'],'label':a['name'],'percent':25} for a in agents()]
    m=default_modeling_requirements(r);m['individual']['agent_types']=agents();m['individual']['execution_contract']=CONFIGURED_LEARNING_CONTRACT
    m['environment']['selected_environment_model_id']='env-memorial-tower-v1'
    m['rules']['accepted_recommended_rule_ids']=m['rules']['recommended_rule_ids'];confirm_modeling(m)
    return compile_artificial_society({'requirement':r,'requirement_id':'req-test','fingerprint':'qa'},m)


class ConfiguredLearningTests(unittest.TestCase):
    def test_all_three_declared_versions_compile_to_effective_fingerprints_without_mutation(self):
        for method in ('linucb_v1','tabular_q_v1','bounded_adaptation_v1'):
            c,s=confirmed_settings(method);before=deepcopy((c,s));out=prepare_role_learning_settings(c,s)
            self.assertEqual(before,(c,s));self.assertTrue(out['learning_config']['feature_schema_version'].startswith('sha256:'))
            self.assertTrue(out['learning_config']['feedback']['version'].startswith('sha256:'));self.assertNotIn('audit_capacity',out)

    def test_incomplete_or_unsupported_runtime_settings_block_confirmation(self):
        c,s=confirmed_settings()
        for key in s:
            bad=deepcopy(s);bad.pop(key)
            with self.subTest(key=key),self.assertRaises(ValueError):prepare_role_learning_settings(c,bad)
        for key,value in [('bootstrap_on_truncation',True),('integration_mode','overwrite'),('storage_capacity',True),('audit_capacity',1)]:
            bad=deepcopy(s);bad[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):prepare_role_learning_settings(c,bad)

    def test_shared_inheritance_and_active_missing_reward_reject(self):
        for kind in ('shared','inherit','goal','bins','candidate'):
            c,s=confirmed_settings()
            if kind=='shared':c['policy_scope']='role'
            if kind=='inherit':c['lifecycle']={'mode':'inherit','cohort_key':'qa','checkpoint_ref':'qa'}
            if kind=='goal':c['feedback']['weights']={'goal_progress':1};s['reward_scales']={'goal_progress':1}
            if kind=='bins':s['encoding']['state_bins']={'perceived_risk':[1,0]}
            if kind=='candidate':s['encoding']['columns']=['candidate_cost'];s['encoding']['scales']={'candidate_cost':1}
            with self.subTest(kind=kind),self.assertRaises(ValueError):prepare_role_learning_settings(c,s)

    def test_same_method_different_scale_changes_configuration_and_effective_binding(self):
        rows=agents();base=build_runtime_binding({'agent_types':rows,'execution_contract':CONFIGURED_LEARNING_CONTRACT})
        old=validate_configured_roles(rows)[0]['social_visitor']['learning_config']['feature_schema_version']
        rows[1]['learning_runtime']['encoding']['scales']['bias']=2
        self.assertNotEqual(old,validate_configured_roles(rows)[0]['social_visitor']['learning_config']['feature_schema_version'])
        self.assertNotEqual(base['configuration_fingerprint'],build_runtime_binding({'agent_types':rows,'execution_contract':CONFIGURED_LEARNING_CONTRACT})['configuration_fingerprint'])

    def test_architecture_projection_and_per_run_capacity_must_match(self):
        rows=agents();validate_assembly(rows,CONFIGURED_LEARNING_CONTRACT)
        bad=deepcopy(rows);bad[0]['architecture'][-1]['method']='经验回放'
        with self.assertRaisesRegex(ValueError,'projection'):validate_assembly(bad,CONFIGURED_LEARNING_CONTRACT)
        bad=deepcopy(rows);bad[0]['learning_runtime']['audit_capacity']=4000
        with self.assertRaisesRegex(ValueError,'capacity'):validate_assembly(bad,CONFIGURED_LEARNING_CONTRACT)

    def test_no_llm_role_or_empty_active_configuration_can_be_accepted(self):
        rows=agents();rows[-1]['architecture'][3]['method']='LLM-driven'
        with self.assertRaisesRegex(ValueError,'rule decisions'):validate_configured_roles(rows)
        rows=agents()
        for a in rows:a['capabilities']['learning']=False
        with self.assertRaisesRegex(ValueError,'learning-enabled'):validate_configured_roles(rows)

    def test_legacy_and_candidate_contract_reject_new_methods_and_fields(self):
        for contract in (None,'candidate-decision-v1','heterogeneous-learning-v1'):
            with self.subTest(contract=contract),self.assertRaises(ValueError):validate_assembly(agents(),contract)
        rows=agents()
        for a in rows:a.pop('learning_config',None);a.pop('learning_runtime',None)
        with self.assertRaisesRegex(ValueError,'v2'):validate_assembly(rows,None)

    def test_pipeline_inherits_unique_candidate_frozen_input_and_goal_prepare(self):
        from tests.test_candidate_decision_stage_four import candidates
        from tests.test_observed_feedback_stage_five import fixtures
        e=IndividualModelingEngine();e.configure(agents(),execution_contract=CONFIGURED_LEARNING_CONTRACT)
        p,s,o=fixtures();cs=candidates(p.person_id,o.snapshot_id)
        self.assertEqual(cs,e.prepare_decision_boundary(p,s,o,cs))
        self.assertEqual(cs,e.prepare_decision_boundary(p,s,o,()))
        self.assertFalse(e._replay_enabled(p));self.assertIsNotNone(e.assemblies['consumer'].learning_settings)

    def test_model_parse_and_fingerprint_bind_full_runtime_spec(self):
        m=model();self.assertEqual(m,ArtificialSocietySpec.parse(m).payload)
        self.assertEqual(CONFIGURED_LEARNING_CONTRACT,m['runtime_binding']['implementation_version'])
        bad=deepcopy(m);bad['individual_model']['agent_types'][0]['learning_runtime']['encoding']['scales']['bias']=2
        with self.assertRaisesRegex(ValueError,'binding'):ArtificialSocietySpec.parse(bad)

    def test_declared_snapshot_edit_without_reconfirmation_is_blocked(self):
        from tests.test_modeling_requirements_compiler import configured_modeling
        r=requirement_payload();m=configured_modeling(r);m['individual']['execution_contract']=CONFIGURED_LEARNING_CONTRACT
        # Original confirmation predates this edit; must fail before any model publication.
        with self.assertRaises(ValueError):compile_artificial_society({'requirement':r,'requirement_id':'req-test','fingerprint':'qa'},m)

    def test_bad_runtime_bind_retains_previous_model_and_engine(self):
        from tests.test_backend_merge_contracts import RuntimeCompatibilityTests
        r=RuntimeCompatibilityTests().runtime();self.addCleanup(r.close)
        prior=RuntimeCompatibilityTests().model(r);r.configure_artificial_society(prior);engine=r.decision_engine
        new=model();new['individual_model']['agent_types'][0]['learning_runtime']['storage_capacity']=0
        with self.assertRaises(ValueError):r.configure_artificial_society({'model_id':'qa','requirement_id':r.requirement_id,'fingerprint':'qa','artificial_society':new})
        self.assertIs(engine,r.decision_engine);self.assertEqual(prior,r.artificial_society_record)

    def test_confirmed_attachment_cannot_override_model_with_arbitrary_settings(self):
        r,c=runtime_stub();r.state=__import__('crowdsim.core.simulation_runtime',fromlist=['RuntimeState']).RuntimeState.READY
        r._async_tick_inflight=None;r.learning_controller=None;r.recorder=Mock();r.class2_flows=None
        r.artificial_society_record={'fingerprint':'qa','artificial_society':{'individual_model':{'agent_types':agents()}}}
        r.decision_engine.configure(agents(),execution_contract=CONFIGURED_LEARNING_CONTRACT)
        arbitrary=validate_configured_roles(agents())[0];arbitrary['consumer']['storage_capacity']=10
        with self.assertRaisesRegex(ValueError,'differs'):SimulationRuntime.attach_learning_controller(r,arbitrary,capacity=4096)
        self.assertIsNone(r.learning_controller)

    def test_dirty_input_blocks_all_contracts_even_with_learning_disabled(self):
        for contract in (None,'candidate-decision-v1',CONFIGURED_LEARNING_CONTRACT):
            rows=agents();rows[0]['learning_draft_dirty']=True;rows[0]['capabilities']['learning']=False
            with self.subTest(contract=contract),self.assertRaisesRegex(ValueError,'not saved'):
                validate_assembly(rows,contract)

    def test_storage_capacity_type_bounds_and_class_two_are_rejected(self):
        c,s=confirmed_settings()
        for value in (0,100001,True,1.5):
            bad=deepcopy(s);bad['storage_capacity']=value
            with self.subTest(value=value),self.assertRaises(ValueError):prepare_role_learning_settings(c,bad)
        r=requirement_payload();r['research']['problem_type']=2
        r['population']['distributions']['crowd_role']=[{'code':a['id'],'label':a['name'],'percent':25} for a in agents()]
        m=default_modeling_requirements(r);m['individual']['agent_types']=agents();m['individual']['execution_contract']=CONFIGURED_LEARNING_CONTRACT
        m['environment']['selected_environment_model_id']='env-memorial-tower-v1';confirm_modeling(m)
        with self.assertRaisesRegex(ValueError,'first-class'):
            compile_artificial_society({'requirement':r,'requirement_id':'req-test','fingerprint':'qa'},m)

if __name__=='__main__':unittest.main()
