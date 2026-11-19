"""Stage-five draft contract only; unimplemented learners must never publish/run."""
from copy import deepcopy
import json
import subprocess
from pathlib import Path
import unittest
from crowdsim.modeling_algorithm.individual_model.learning.configuration import (
    METHODS, LEARNING_CONTRACT, learning_catalog, validate_learning_config, require_learning_ready)
from crowdsim.modeling_algorithm.individual_model.module_assembly import validate_assembly
from crowdsim.modeling_algorithm.individual_model.pipeline import IndividualModelingEngine
from crowdsim.modeling_algorithm.runtime_binding import build_runtime_binding
from crowdsim.modeling_algorithm.compiler import executable_catalog, _compile_agent, compile_artificial_society
from crowdsim.domain.artificial_society_spec import ArtificialSocietySpec
from tests.test_modeling_binding_stage_one import compiled_model
from tests.test_modeling_requirements_compiler import requirement_payload, configured_modeling, confirm_modeling


def config(method='linucb_v1'):
    bounds=METHODS[method]['parameters']
    return dict(method_id=method,implementation_version='1',targets=['goal_route','action'],
        feature_schema_version='qa-features-v1',action_schema_version='qa-actions-v1',
        parameters={k:(1 if kind=='integer' else .5 if hi<=1 else 1) for k,(lo,hi,kind) in bounds.items()},
        experience_scope='individual',policy_scope='individual',lifecycle={'mode':'reset'},
        feedback={'rule_id':'qa-feedback','version':'1','weights':{'goal_progress':1}})


class LearningConfigurationTests(unittest.TestCase):
    def test_three_method_schemas_are_draft_only_and_detached(self):
        self.assertEqual(3,len(learning_catalog()))
        self.assertTrue(all(not r['implemented'] and not r['runtime_ready'] for r in learning_catalog()))
        r=learning_catalog();r[0]['parameters'].clear()
        self.assertTrue(learning_catalog()[0]['parameters'])
        entries = [r for r in executable_catalog() if r.get('method_id')]
        self.assertEqual(3, len(entries))
        self.assertTrue(all(r['execution_contract'] == 'heterogeneous-learning-v2' for r in entries))
        # Old draft schema/gate remains unchanged; executable entries require v2.

    def test_each_complete_config_validates_but_never_implies_ready(self):
        for method in METHODS:
            c=config(method);self.assertEqual(c,validate_learning_config(c,complete=True))
            with self.assertRaisesRegex(ValueError,'not implemented'):require_learning_ready(c)

    def test_partial_validates_without_defaults_and_complete_rejects_missing(self):
        self.assertEqual({},validate_learning_config({}))
        self.assertEqual({'method_id':'linucb_v1'},validate_learning_config({'method_id':'linucb_v1'}))
        with self.assertRaisesRegex(ValueError,'incomplete'):validate_learning_config({'method_id':'linucb_v1'},complete=True)

    def test_unknown_null_bool_nonfinite_and_wrong_method_params_reject(self):
        bads=[None,[],False,{'method_id':[]},{'method_id':'__proto__'}, {'method_id':'linucb_v1','parameters':{'epsilon':.1}},
            {'method_id':'linucb_v1','parameters':{'update_every':True}},
            {'method_id':'linucb_v1','parameters':{'regularization':float('inf')}},
            {'method_id':'linucb_v1','parameters':{'update_budget':1.5}},
            {'targets':['action','action']},{'feedback':{'weights':{'risk_exposure':-1}}},
            {'lifecycle':{'mode':'reset','checkpoint_ref':'qa'}},{'unexpected':'x'}]
        for bad in bads:
            with self.subTest(bad=bad),self.assertRaises(ValueError):validate_learning_config(bad)

    def test_replay_is_optional_method_specific_and_capacity_checked(self):
        for replay in [dict(enabled=True,capacity=10,batch_size=11),dict(enabled=False,capacity=10),dict(capacity=10)]:
            c=config('tabular_q_v1');c['replay']=replay
            with self.assertRaises(ValueError):validate_learning_config(c,complete=True)
        c=config('linucb_v1');c['replay']={'enabled':False}
        with self.assertRaises(ValueError):validate_learning_config(c)
        c=config('tabular_q_v1');c['replay']={'enabled':True,'capacity':10,'batch_size':2}
        self.assertEqual(c,validate_learning_config(c,complete=True))

    def test_checkpoint_and_individual_cohort_required_for_inheritance(self):
        c=config();c['lifecycle']={'mode':'inherit','checkpoint_ref':'qa'}
        with self.assertRaisesRegex(ValueError,'cohort'):validate_learning_config(c,complete=True)
        c['lifecycle']['cohort_key']='qa-cohort';validate_learning_config(c,complete=True)
        c['lifecycle']={'mode':'frozen'}
        with self.assertRaisesRegex(ValueError,'checkpoint'):validate_learning_config(c,complete=True)

    def test_compiler_projection_and_fingerprint_include_learning_but_do_not_mutate(self):
        m=compiled_model();before=deepcopy(m);base=build_runtime_binding(m['individual_model'])
        agent=m['individual_model']['agent_types'][0];agent['learning_config']=config()
        projected=_compile_agent(agent)
        self.assertEqual(agent['learning_config'],projected['learning_config'])
        binding=build_runtime_binding(m['individual_model'])
        self.assertNotEqual(base['configuration_fingerprint'],binding['configuration_fingerprint'])
        projected['learning_config']['targets'].clear()
        self.assertTrue(agent['learning_config']['targets'])
        self.assertNotIn('learning_config',before['individual_model']['agent_types'][0])

    def test_legacy_contract_cannot_silently_ignore_learning_config(self):
        agents=compiled_model()['individual_model']['agent_types'];agents[0]['learning_config']=config()
        for version in (None,'module-assembly-v1','desire-goal-v1','candidate-decision-v1'):
            with self.assertRaisesRegex(ValueError,'requires'):validate_assembly(agents,version)
        with self.assertRaisesRegex(ValueError,'incomplete'):validate_assembly(agents,LEARNING_CONTRACT)

    def test_reconfigure_rejection_is_atomic(self):
        agents=compiled_model()['individual_model']['agent_types'];e=IndividualModelingEngine()
        e.configure(agents,execution_contract='candidate-decision-v1');old=e.assemblies;old_contract=e.execution_contract
        for a in agents:
            if a['capabilities']['learning']:a['learning_config']=config()
        with self.assertRaisesRegex(ValueError,'not implemented'):e.configure(agents,execution_contract=LEARNING_CONTRACT)
        self.assertIs(old,e.assemblies);self.assertEqual(old_contract,e.execution_contract)

    def test_complete_draft_cannot_compile_and_forged_model_cannot_parse(self):
        r=requirement_payload();draft=configured_modeling(r);draft['individual']['execution_contract']=LEARNING_CONTRACT
        for a in draft['individual']['agent_types']:
            if a['capabilities']['learning']:a['learning_config']=config()
        confirm_modeling(draft)
        with self.assertRaisesRegex(ValueError,'not implemented'):
            compile_artificial_society({'requirement':r,'requirement_id':'qa','fingerprint':'sha256:qa'},draft)
        model=compiled_model();model['individual_model']['execution_contract']=LEARNING_CONTRACT
        for a in model['individual_model']['agent_types']:
            if a['capabilities']['learning']:a['learning_config']=config()
        model['runtime_binding']=build_runtime_binding(model['individual_model'])
        with self.assertRaisesRegex(ValueError,'not implemented'):ArtificialSocietySpec.parse(model)

    def test_python_and_browser_validation_parity(self):
        front=Path(__file__).resolve().parents[2]/'CrowdSim/frontend'
        c=config('tabular_q_v1')
        cases=[{}, {'method_id':'linucb_v1'}, c,dict(c,parameters=dict(c['parameters'],update_every=True)),
               dict(c,parameters=dict(c['parameters'],epsilon=2)),dict(c,replay=dict(enabled=True,capacity=2,batch_size=3)),
               dict(c,lifecycle={'mode':'inherit','checkpoint_ref':'qa'}),{'method_id':None}]
        expected=[]
        for value in cases:
            try:validate_learning_config(value,complete=True);expected.append(True)
            except ValueError:expected.append(False)
        script="import {validateLearningConfiguration as v} from './src/views/crowdSim/DuringAnalysis/chat/learningConfiguration.js';let s='';for await(const c of process.stdin)s+=c;console.log(JSON.stringify(JSON.parse(s).map(c=>{try{v(c,{complete:true});return true}catch{return false}})))"
        actual=json.loads(subprocess.check_output(['node','--input-type=module','-e',script],cwd=front,input=json.dumps(cases),text=True))
        self.assertEqual(expected,actual)

if __name__=='__main__':unittest.main()
