"""Execution checks must finish before the UI can announce a successful check."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from postanalysis_api.api.common import Request
from postanalysis_api.api.datasets import handle
from postanalysis_api.schemas.common import ApiError
from postanalysis_api.services.execution_validation import validate_execution_input


class ExecutionValidationTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.dataset = json.loads((root / 'docs/examples/shanghai_memorial_post_dataset.json').read_text(encoding='utf-8'))
        self.dataset.update(id='ds-one', version='a' * 64)
        self.app = SimpleNamespace(executor=SimpleNamespace(binary='sumo'), _check_start_capability=Mock())

    def test_success_compiles_baseline_and_every_control_group_and_checks_native_sumo(self):
        before = deepcopy(self.dataset)
        with patch('postanalysis_api.services.execution_validation.compile_routes') as compile_, \
                patch('postanalysis_api.services.execution_validation.subprocess.run', return_value=SimpleNamespace(returncode=0)) as run:
            result = validate_execution_input(self.app, self.dataset)
        self.assertTrue(result['ready'])
        self.assertEqual(self.dataset['version'], result['datasetVersion'])
        self.assertEqual(6, compile_.call_count)  # F-00 and the default five controls.
        self.app._check_start_capability.assert_called_once()
        self.assertEqual('0.5', run.call_args.args[0][run.call_args.args[0].index('--end') + 1])
        self.assertEqual(before, self.dataset)

    def test_missing_inputs_and_invalid_profile_cannot_pass_schema_only_checks(self):
        for mutate in (lambda d: d['metadata'].pop('sourceRecords'),
                       lambda d: d['metadata']['executionProfile'].update(baselineControl={}),
                       lambda d: d['metadata']['sourceRecords'][0].update(intervalEntered=-1)):
            data = deepcopy(self.dataset); mutate(data)
            with self.subTest(mutate=mutate), patch('postanalysis_api.services.execution_validation.compile_routes') as compile_:
                result = validate_execution_input(self.app, data)
                self.assertFalse(result['ready'])
                self.assertTrue(result['missing'])
                compile_.assert_not_called()

    def test_route_or_capability_failure_cannot_be_announced_as_ready(self):
        self.app._check_start_capability.side_effect = ApiError('CAPABILITY_UNAVAILABLE', 'SUMO unavailable', 503)
        result = validate_execution_input(self.app, self.dataset)
        self.assertFalse(result['ready'])
        self.assertEqual('CAPABILITY_UNAVAILABLE', result['code'])
        self.app._check_start_capability.side_effect = None
        with patch('postanalysis_api.services.execution_validation.compile_routes', side_effect=ApiError('ROUTE_UNREACHABLE', 'route missing', 422)):
            self.assertEqual('ROUTE_UNREACHABLE', validate_execution_input(self.app, self.dataset)['code'])

    def test_missing_binary_rejected_input_and_native_timeout_fail_before_success(self):
        for outcome in (SimpleNamespace(returncode=1), subprocess.TimeoutExpired('sumo', 20)):
            with self.subTest(outcome=outcome), patch('postanalysis_api.services.execution_validation.compile_routes'), \
                    patch('postanalysis_api.services.execution_validation.subprocess.run') as run:
                if isinstance(outcome, Exception): run.side_effect = outcome
                else: run.return_value = outcome
                self.assertFalse(validate_execution_input(self.app, self.dataset)['ready'])
        self.app.executor.binary = None
        with patch('postanalysis_api.services.execution_validation.compile_routes'):
            self.assertEqual('SUMO_BINARY_MISSING', validate_execution_input(self.app, self.dataset)['code'])

    def test_validation_endpoint_uses_current_user_workspace_and_dataset_version(self):
        self.app.repo = SimpleNamespace(get=Mock(return_value=self.dataset))
        request = Request('GET', '/datasets/ds-one/validation', {}, {},
                          {'X-CrowdSim-User': 'u', 'X-CrowdSim-Workspace': 'post-c1'}, self.app)
        with patch('postanalysis_api.services.execution_validation.compile_routes'), \
                patch('postanalysis_api.services.execution_validation.subprocess.run', return_value=SimpleNamespace(returncode=0)):
            status, result, _ = handle(request)
        self.assertEqual(200, status)
        self.assertTrue(result['ready'])
        self.app.repo.get.assert_called_once_with('datasets', 'ds-one', 'u', 'post-c1')
