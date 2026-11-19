from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock
import xml.etree.ElementTree as ET

from postanalysis_api.schemas.common import ApiError
from postanalysis_api.workers.native_runtime import VERSION, compile_input, execute, native_action, validate_task, archive_evidence, read_evidence
from test_post_population_execution import population_dataset


class NativeRuntimeTests(unittest.TestCase):
    def test_native_group_roundtrips_and_two_changed_parameters_fail_ofat(self):
        from postanalysis_api.schemas.execution import validate_group_execution
        from postanalysis_api.workers.native_runtime import changed_actions
        action = {'id': 'limit', 'actionType': 'set_inflow_rate', 'atSeconds': 1,
                  'target': {'entryId': 'monument_m3'}, 'parameters': {'rate': .5, 'durationSeconds': 2}}
        value = {'referenceDesignId': 'F-00', 'actions': [action]}
        self.assertEqual(value, validate_group_execution(value, 5))
        changed = deepcopy(action)
        changed['parameters']['rate'] = .8
        changed['atSeconds'] = 2
        self.assertEqual(2, len(changed_actions([action], [changed])))
        task = {'groups': [{'id': 'CF-01', 'experimentType': 'observation', 'interventions': [action]},
                          {'id': 'CF-02', 'referenceGroupId': 'CF-01', 'experimentType': 'intervention', 'interventions': [changed]}]}
        with self.assertRaises(ApiError) as error:
            validate_task(task, population_dataset(5))
        self.assertEqual('INTERVENTION_NOT_ISOLATED', error.exception.code)

    def test_legacy_controls_and_completed_worlds_cannot_be_mixed(self):
        data = population_dataset(5)
        with self.assertRaises(ApiError) as error:
            native_action({'id': 'old', 'actionType': 'guidance', 'parameters': {}})
        self.assertEqual('NATIVE_ACTION_MAPPING_REQUIRED', error.exception.code)
        with self.assertRaises(ApiError) as error:
            validate_task({}, data, [{'status': 'completed'}])
        self.assertEqual('EXECUTION_PROTOCOL_MISMATCH', error.exception.code)

    def test_compile_keeps_initial_people_identical_and_treats_only_at_runtime(self):
        data = population_dataset(5)
        before = deepcopy(data)
        action = {'id': 'guide', 'actionType': 'publish_guidance', 'atSeconds': 1,
                  'target': {'regionId': data['metadata']['factualContext']['hotspot']['id']}, 'parameters': {'message': '请有序离开', 'command': 'disperse'}}
        with TemporaryDirectory() as folder:
            first, second = Path(folder) / 'base.xml', Path(folder) / 'cf.xml'
            left = compile_input(data, 42, [], 5, first)
            right = compile_input(data, 42, [action], 5, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(left['cohorts'], right['cohorts'])
            root = ET.parse(first).getroot()
            self.assertFalse(root.findall('.//stop'))
            self.assertEqual(20, len(root.findall('person')))
            self.assertTrue(all(p.find("param[@key='crowdsim.hotspot_id']") is not None for p in root.findall('person')))
        self.assertEqual(before, data)

    def test_artifact_failure_removes_uploaded_orphans(self):
        repo = MagicMock()
        repo.files.upload_from_stream.return_value = 'file-one'
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'evidence.jsonl'
            path.write_text('{}\n', encoding='utf-8')
            with self.assertRaises(OSError):
                archive_evidence(repo, {'ownerId': 'u', 'workspaceId': 'w', 'runId': 'r'},
                    {'runtimeDirectory': folder, 'evidencePath': str(path)})
        repo.files.delete.assert_called_once_with('file-one')

    @unittest.skipUnless(os.environ.get('SUMO_BINARY'), 'set SUMO_BINARY for isolated real-SUMO verification')
    def test_real_runtime_records_actual_motion_messages_and_decisions_and_closes(self):
        data = population_dataset(5)
        action = {'id': 'guide', 'actionType': 'publish_guidance', 'atSeconds': 1,
                  'target': {'regionId': data['metadata']['factualContext']['hotspot']['id']},
                  'parameters': {'message': '请有序离开', 'command': 'disperse', 'durationSeconds': 4}}
        with TemporaryDirectory() as folder:
            folder = Path(folder)
            path = folder / 'routes.xml'
            manifest = compile_input(data, 42, [action], 5, path)
            manifest['seed'] = 42
            output = execute(os.environ['SUMO_BINARY'], path, folder,
                ['--begin', '0', '--end', '5', '--step-length', '.5', '--no-step-log', 'true',
                 '--time-to-teleport', '-1'], manifest, 5, lambda: False)
            self.assertEqual(VERSION, output['executionProtocol'])
            self.assertGreater(output['evidenceCounts']['motion'], 20)
            self.assertGreater(output['evidenceCounts']['message'], 0)
            self.assertGreater(output['evidenceCounts']['decision'], 0)
            self.assertEqual(0, output['pendingActionCount'])
            rows = [json.loads(line) for line in Path(output['evidencePath']).read_text(encoding='utf-8').splitlines()]
            self.assertTrue(any(row['kind'] == 'motion' and row['acceleration'] is not None for row in rows))
            self.assertTrue(any(row['kind'] == 'motion' and row['knownEvents'] for row in rows))
            decisions = [row for row in rows if row['kind'] == 'decision']
            self.assertIn('observation', decisions[0]['context'])
            self.assertIn('status', decisions[0]['execution'])
        import traci
        self.assertFalse(traci.connection._connections)

    def test_native_initial_model_reply_uses_only_the_new_action_contract(self):
        from test_post_unified_design import native_fixture
        from dialogue_service.post_agent import validate_reply
        _, plan = native_fixture()
        design = plan['counterfactualDesign']
        design['groups'] = design['groups'][:1]
        context = {'designSchemaVersion': 2, 'designOperation': 'initial_group', 'nativeActionContract': 'crowdsim.action_contract/1.0',
                   'executionCapabilities': True, 'counterfactualDesign': design, 'variableDefinitions': plan['variableDefinitions']}
        reply = {'assistant_message': '请核对原生动作', 'action': 'update_design', 'group_updates': deepcopy(design['groups']), 'repeat_count': 20}
        result = validate_reply('design', reply, context)
        self.assertIn('actions', result['group_updates'][0]['execution'])
        reply['group_updates'][0]['execution']['actions'][0]['atSeconds'] = 90000
        with self.assertRaises(ValueError):
            validate_reply('design', reply, context)

    @unittest.skipUnless(os.environ.get('SUMO_BINARY'), 'set SUMO_BINARY for isolated real-SUMO verification')
    def test_batch_archives_behavior_in_gridfs_and_reads_it_with_owner_scope(self):
        import mongomock
        import mongomock.gridfs
        from postanalysis_api.repositories.mongodb import MongoRepository
        from postanalysis_api.workers.sumo_batch import SumoBatchExecutor
        from postanalysis_api.workers.analysis import summarize
        import threading
        mongomock.gridfs.enable_gridfs_integration()
        repo = MongoRepository(mongomock.MongoClient().native_runtime_test)
        repo.files._timeout = None  # PyMongo CSOT + mongomock GridFSBucket compatibility.
        data = population_dataset(3)
        task = {'id': 'sim-native', 'ownerId': 'u', 'workspaceId': 'w', 'datasetVersion': data['version'],
                'scenario': {'durationSeconds': 3}, 'metricIds': ['peak_density', 'mean_speed'], 'pcFeatures': []}
        with TemporaryDirectory() as folder:
            executor = SumoBatchExecutor(repo, folder)
            result = executor._one(task, data, 'F-00', 'baseline', 42, [], threading.Event())
            self.assertEqual('completed', result['status'])
            self.assertEqual(VERSION, result['executionProtocol'])
            self.assertTrue(result['timeline'])
            self.assertGreater(result['behaviorEvidence']['counts']['decision'], 0)
            self.assertTrue(result['behaviorEvidence']['artifacts'])
            chunk = read_evidence(repo, result['id'], 'u', 'w', 'behavior_evidence.jsonl', limit=20)
            self.assertEqual(20, chunk['nextOffset'])
            with self.assertRaises(ApiError):
                read_evidence(repo, result['id'], 'other', 'w', 'behavior_evidence.jsonl')
            self.assertEqual(20, summarize(task, [result])['metrics']['runs'][0]['measurement']['plannedPersonCount'])
            executor.close()

    @unittest.skipUnless(os.environ.get('SUMO_BINARY'), 'set SUMO_BINARY for isolated real-SUMO verification')
    def test_real_runtime_cancellation_closes_traci_and_evidence_file(self):
        import traci
        data = population_dataset(5)
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'routes.xml'
            manifest = compile_input(data, 42, [], 5, path)
            manifest['seed'] = 42
            with self.assertRaises(InterruptedError):
                execute(os.environ['SUMO_BINARY'], path, folder, ['--step-length', '.5', '--no-step-log', 'true'],
                        manifest, 5, lambda: True)
            # This also verifies the writer handle was released on failure.
            (Path(folder) / 'behavior_evidence.jsonl').rename(Path(folder) / 'cancelled.jsonl')
        self.assertFalse(traci.connection._connections)


if __name__ == '__main__':
    unittest.main()
