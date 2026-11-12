"""Offline storage and handler checks; no HTTP listener, model or simulator."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from unittest.mock import patch

from dialog_storage.during_requirements import DuringRequirementRepository, RequirementConflict
from dialog_storage.during_workflow import experiment_group_id
from dialog_storage.repository import ConversationRepository
from dialogue_service.server import DialogueHandler, _allowed_origins


def state(note="test"):
    requirement = {"schema_version": 2, "research": {"problem_type": 1}, "project": {"title": "test"}}
    modeling = {"version": 1, "population": {}}
    return {"requirement": {"version": 3, "view": "requirement", "draft": {
        "id": "draft-one", "revision": 15, "requirement": requirement,
        "confirmation": {"id": "snapshot-one"}, "savedSnapshotId": "snapshot-one"},
        "snapshots": [{"id": "snapshot-one", "draftId": "draft-one", "revision": 15, "requirement": deepcopy(requirement), "modeling_requirements": modeling}],
        "modelingRequirements": deepcopy(modeling), "assistantTurns": [{"role": "assistant", "content": note}],
        "submissions": {"snapshot-one": {"requirement_id": "req-one", "fingerprint": "sha256:one"}}},
        "observation": {"entries": [{"conversationId": "study", "clientRequestId": "request-one", "snapshotId": "snapshot-one", "modelId": "model-one", "experimentId": None}], "selectedId": "request-one", "preparedModels": {}}, "legacy": None}


class DuringStorageTests(unittest.TestCase):
    def test_local_frontend_origins_include_active_development_ports_and_env_extensions(self):
        with patch.dict("os.environ", {"CROWDSIM_DIALOG_ALLOWED_ORIGINS": "http://example.test:7000/"}, clear=False):
            origins = _allowed_origins()
        for origin in ("http://localhost:8080", "http://localhost:8081", "http://127.0.0.1:8081", "http://localhost:9090"):
            self.assertIn(origin, origins)
        self.assertIn("http://example.test:7000", origins)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = DuringRequirementRepository(self.root / "during-requirements")

    def save(self, value=None, revision=0, mutation="first"):
        return self.repo.save("study", state=value or state(), expected_revision=revision, mutation_id=mutation)

    def test_full_record_survives_restart_and_does_not_depend_on_chat_sync(self):
        accepted = self.save()
        reopened = DuringRequirementRepository(self.repo.root)
        self.assertEqual(accepted, reopened.load("study"))
        chats = ConversationRepository(self.root)
        chats.replace_all([{"id": "post-study", "mode": "post", "messages": []}])
        chats.replace_all([])
        self.assertEqual(accepted, reopened.load("study"))
        self.assertIsNone(reopened.load("another-study"))

    def test_hotspot_times_are_frozen_and_open_end_is_supported_in_a_separate_conversation(self):
        value = state()
        requirement = value["requirement"]["draft"]["requirement"]
        requirement.update(research={"problem_type": 1}, scenario={"hotspot_start_seconds": 900, "hotspot_end_seconds": 1200},
                           experiment={"duration_seconds": 60, "repetitions": 3})
        value["requirement"]["snapshots"][0]["requirement"] = deepcopy(requirement)
        self.save(value)
        requirement["scenario"]["hotspot_end_seconds"] = None
        value["requirement"]["draft"].update(revision=16, confirmation=None, savedSnapshotId=None)
        with self.assertRaises(ValueError):
            self.save(value, 1, "open-end")
        restored = DuringRequirementRepository(self.repo.root).load("study")["state"]["requirement"]
        self.assertEqual(1200, restored["draft"]["requirement"]["scenario"]["hotspot_end_seconds"])
        self.assertEqual(1200, restored["snapshots"][0]["requirement"]["scenario"]["hotspot_end_seconds"])
        self.assertEqual(60, restored["draft"]["requirement"]["experiment"]["duration_seconds"])
        value["requirement"]["draft"].update(confirmation={"id": "snapshot-one"}, savedSnapshotId="snapshot-one")
        value["requirement"]["snapshots"][0]["requirement"] = deepcopy(requirement)
        value["observation"]["entries"][0]["conversationId"] = "open-study"
        self.repo.save("open-study", state=value, expected_revision=0, mutation_id="open-end")
        self.assertIsNone(self.repo.load("open-study")["state"]["requirement"]["snapshots"][0]["requirement"]["scenario"]["hotspot_end_seconds"])

    def test_same_mutation_is_idempotent_but_cannot_change_its_payload(self):
        accepted = self.save()
        self.assertEqual(accepted, self.save())
        with self.assertRaises(ValueError):
            self.save(state("changed"))

    def test_only_one_simultaneous_writer_can_save_each_revision(self):
        self.save()
        def write(i):
            try:
                return self.save(state(str(i)), 1, f"change-{i}")
            except RequirementConflict as error:
                return error
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(write, [1, 2]))
        self.assertEqual(1, sum(isinstance(result, RequirementConflict) for result in results))
        self.assertEqual(2, self.repo.load("study")["revision"])
        with self.assertRaises(RequirementConflict) as conflict:
            self.save(state("stale"), 1, "stale-change")
        self.assertEqual(2, conflict.exception.record["revision"])

    def test_frozen_snapshots_receipts_and_experiment_bindings_cannot_be_lost(self):
        self.save()
        mutations = [
            lambda s: s["requirement"].update(snapshots=[]),
            lambda s: s["requirement"]["snapshots"][0].update(revision=16),
            lambda s: s["requirement"].update(submissions={}),
            lambda s: s["observation"].update(entries=[]),
            lambda s: s["observation"]["entries"][0].update(modelId="wrong"),
            lambda s: s["observation"]["entries"][0].update(conversationId="other"),
        ]
        for mutation in mutations:
            value = state(); mutation(value)
            with self.assertRaises(ValueError):
                self.save(value, 1, "invalid")
        value = state(); value["observation"]["entries"][0]["experimentId"] = experiment_group_id("study")
        self.save(value, 1, "receipt")
        value["observation"]["entries"][0]["experimentId"] = "wrong"
        with self.assertRaises(ValueError):
            self.save(value, 2, "wrong")

    def test_invalid_ids_versions_and_corrupt_records_fail_without_replacing_data(self):
        for cid in ("../escape", "", "a/b"):
            with self.assertRaises(ValueError):
                self.repo.load(cid)
        self.save()
        for revision in (None, -1, True, "1"):
            with self.assertRaises(ValueError):
                self.save(revision=revision)
        value = state(); value["requirement"]["draft"]["requirement"]["schema_version"] = 1
        with self.assertRaises(ValueError):
            self.save(value, 1, "invalid-schema")
        path = self.repo.root / "study.json"
        path.write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.save(state(), 0, "cannot-reset-corrupt")
        self.assertEqual("broken", path.read_text())

    def handler(self, path, payload=None):
        handler = DialogueHandler.__new__(DialogueHandler)
        handler.path = path
        handler.server = SimpleNamespace(during_requirements=self.repo)
        handler.server.repository = ConversationRepository(self.root)
        handler.server.workbench_repository = ConversationRepository(self.root / 'workbench')
        handler._permitted = lambda: True
        body = json.dumps(payload or {}).encode()
        handler.headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        handler.rfile = BytesIO(body)
        responses = []
        handler._send = lambda status, payload: responses.append((status, deepcopy(payload)))
        return handler, responses

    def test_real_handlers_return_records_conflicts_and_read_errors(self):
        path = "/during/conversations/study/requirements"
        get, responses = self.handler(path); get.do_GET()
        self.assertEqual([(200, {"record": None})], responses)
        post, responses = self.handler(path, {"state": state(), "expectedRevision": 0, "mutationId": "first"}); post.do_POST()
        self.assertEqual(200, responses[0][0])
        post, responses = self.handler(path, {"state": state("stale"), "expectedRevision": 0, "mutationId": "stale"}); post.do_POST()
        self.assertEqual(409, responses[0][0]); self.assertEqual(1, responses[0][1]["record"]["revision"])
        (self.repo.root / "study.json").write_text("broken", encoding="utf-8")
        get, responses = self.handler(path); get.do_GET()
        self.assertEqual(503, responses[0][0])

    def test_single_conversation_upsert_does_not_delete_other_phase_history(self):
        conversations = [{"id": phase, "mode": phase, "messages": [{"role": "user", "content": phase}]} for phase in ("pre", "post")]
        post, responses = self.handler('/conversations/sync', {"conversations": conversations}); post.do_POST()
        self.assertEqual(200, responses[0][0])
        during = {"id": "during", "mode": "during", "messages": [{"role": "user", "content": "during"}]}
        post, responses = self.handler('/conversations/upsert', {"conversation": during}); post.do_POST()
        self.assertEqual(200, responses[0][0])
        get, responses = self.handler('/conversations'); get.do_GET()
        self.assertEqual({"pre", "post", "during"}, {item['id'] for item in responses[0][1]['conversations']})

    def test_existing_chat_routes_still_preserve_pre_and_post_history(self):
        conversations = [{"id": phase, "mode": phase, "messages": [{"role": "user", "content": phase}]} for phase in ("pre", "post")]
        post, responses = self.handler('/conversations/sync', {"conversations": conversations}); post.do_POST()
        self.assertEqual(200, responses[0][0])
        before = {p.name: p.read_bytes() for p in self.root.glob('*.json')}
        self.save()
        get, responses = self.handler('/conversations'); get.do_GET()
        self.assertEqual({"pre", "post"}, {item['id'] for item in responses[0][1]['conversations']})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.glob('*.json')})
