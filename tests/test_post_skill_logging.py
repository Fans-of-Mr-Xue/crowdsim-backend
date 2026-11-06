"""Per-answer skill selection is visible in console and persisted dialogue logs."""
import io
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from dialogue_service.post_agent import PostAgentUnavailable, respond
from test_post_dialogue_agent import request_payload


def trace_records(records):
    return [json.loads(record.getMessage().split('skill_trace ', 1)[1])
            for record in records if 'PostAgent skill_trace ' in record.getMessage()]


class PostSkillLoggingTests(unittest.TestCase):
    def test_actual_selected_skills_are_logged_before_the_model_and_returned_unchanged(self):
        payload = request_payload()
        payload['context'].update(conversationId='conv-one', designStage='counterfactual')
        def complete(*args):
            events = trace_records(capture.records)
            self.assertEqual(['skills_loaded', 'model_started'], [event['event'] for event in events])
            return {'assistant_message': '已回答用户问题。'}
        with patch.dict('os.environ', {'CROWDSIM_POST_LLM_MODEL': 'configured-model'}), self.assertLogs('dialogue_service.post_agent', level='INFO') as capture:
            result = respond(payload, complete=complete)
        events = trace_records(capture.records)
        self.assertEqual(['skills_loaded', 'model_started', 'model_completed'], [event['event'] for event in events])
        self.assertEqual(['post_causal_hypothesis', 'counterfactual_design'], result['skills'])
        self.assertEqual(1, len({event['request_id'] for event in events}))
        for event in events:
            self.assertEqual(result['skills'], event['skills'])
            self.assertEqual('configured-model', event['model'])
            self.assertEqual('conv-one', event['conversationId'])
            self.assertEqual('counterfactual', event['designStage'])
            self.assertNotIn('messages', event)
            self.assertNotIn('prompt', event)
        self.assertGreaterEqual(events[-1]['elapsed_ms'], 0)

    def test_retry_keeps_one_request_id_and_logs_both_attempts(self):
        complete = Mock(side_effect=[{'assistant_message': None}, {'assistant_message': '格式已修正。'}])
        with self.assertLogs('dialogue_service.post_agent', level='INFO') as capture:
            result = respond(request_payload('intake'), complete=complete)
        events = trace_records(capture.records)
        self.assertTrue(result['responseRepaired'])
        self.assertEqual(['skills_loaded', 'model_started', 'model_retry', 'model_started', 'model_completed'], [event['event'] for event in events])
        self.assertEqual([1, 2], [event['attempt'] for event in events if event['event'] == 'model_started'])
        self.assertEqual(1, len({event['request_id'] for event in events}))
        self.assertTrue(all(event['skills'] == ['post_data_assessment'] for event in events))

    def test_transport_and_exhausted_format_failures_are_logged_and_still_raise(self):
        for complete, expected_attempts in ((Mock(side_effect=PostAgentUnavailable('offline')), 1),
                                            (Mock(return_value={'assistant_message': None}), 2)):
            with self.subTest(attempts=expected_attempts), self.assertLogs('dialogue_service.post_agent', level='INFO') as capture:
                with self.assertRaises(PostAgentUnavailable):
                    respond(request_payload('intake'), complete=complete)
            events = trace_records(capture.records)
            self.assertEqual('model_failed', events[-1]['event'])
            self.assertEqual(expected_attempts, events[-1]['attempt'])
            self.assertEqual(expected_attempts, complete.call_count)
            self.assertNotIn('model_completed', [event['event'] for event in events])

    def test_each_answer_has_an_independent_request_id(self):
        with self.assertLogs('dialogue_service.post_agent', level='INFO') as capture:
            for _ in range(2):
                respond(request_payload('intake'), complete=lambda *args: {'assistant_message': '已答复。'})
        starts = [event for event in trace_records(capture.records) if event['event'] == 'model_started']
        self.assertEqual(2, len({event['request_id'] for event in starts}))

    def test_existing_logging_setup_writes_the_same_trace_to_console_and_file(self):
        from dialogue_service import logging_setup
        root = logging.getLogger()
        previous_handlers, previous_level = root.handlers[:], root.level
        console = io.StringIO()
        try:
            with TemporaryDirectory() as directory, patch.object(logging_setup, 'LOG_PATH', Path(directory) / 'service.log'), \
                    patch.object(logging_setup.sys, 'stdout', console):
                path = logging_setup.configure_logging()
                respond(request_payload('intake'), complete=lambda *args: {'assistant_message': '已答复。'})
                for handler in root.handlers:
                    handler.flush()
                file_trace = [line.split('PostAgent skill_trace ', 1)[1] for line in path.read_text(encoding='utf-8').splitlines() if 'PostAgent skill_trace ' in line]
                console_trace = [line.split('PostAgent skill_trace ', 1)[1] for line in console.getvalue().splitlines() if 'PostAgent skill_trace ' in line]
                self.assertEqual(3, len(file_trace))
                self.assertEqual(file_trace, console_trace)
                for handler in root.handlers:
                    handler.close()
        finally:
            root.handlers[:] = previous_handlers
            root.setLevel(previous_level)
