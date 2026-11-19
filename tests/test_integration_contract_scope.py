"""Integration contract scope: replay buffer is not a v2 learner; LLM readiness is presence-only.

QA bindings only. They do not replace production defaults and do not open a live model
connection; the real call path remains the existing httpx OpenAI-compatible request.
"""
import json
import os
import unittest

from crowdsim.modeling_algorithm.compiler import executable_catalog
from crowdsim.modeling_algorithm.registry import algorithm_catalog
from crowdsim.modeling_algorithm.individual_model.decision.llm_driven import LLMDrivenDecisionModel

V2_CONTRACT = "heterogeneous-learning-v2"


def entries(module: str):
    return {(it["module"], it["method"]): it for it in algorithm_catalog() if it["model"] == "individual" and it["module"] == module}


class ReplayScopeTests(unittest.TestCase):
    def test_replay_is_legacy_reward_buffer_not_v2_learner(self):
        replay = entries("learning").get(("learning", "经验回放"))
        self.assertIsNotNone(replay)
        self.assertTrue(replay["implemented"])
        self.assertEqual(replay["scope"], "legacy")
        self.assertEqual(replay["policy_kind"], "reward_stat_buffer")
        self.assertFalse(replay["runtime_ready"])
        self.assertNotEqual(replay.get("execution_contract"), V2_CONTRACT)

        v2 = [it for it in entries("learning").values() if it.get("execution_contract") == V2_CONTRACT]
        self.assertEqual(3, len(v2))
        self.assertTrue(all(it["runtime_ready"] for it in v2))
        replay_methods = {it["method"] for it in v2}
        self.assertNotIn("经验回放", replay_methods)

    def test_v2_whitelist_excludes_replay_while_legacy_keeps_it(self):
        catalog = [it for it in executable_catalog() if it["model"] == "individual"]
        v2_learning = {it["method"] for it in catalog
                       if it["module"] == "learning" and it.get("execution_contract") == V2_CONTRACT}
        legacy_learning = {it["method"] for it in catalog
                           if it["module"] == "learning" and not it.get("execution_contract")}
        self.assertNotIn("经验回放", v2_learning)
        self.assertIn("经验回放", legacy_learning)


class LlmReadinessTests(unittest.TestCase):
    def test_default_offline_and_diagnostics_never_emit_key(self):
        old = os.environ.get("CROWDSIM_MODEL_API_KEY")
        os.environ.pop("CROWDSIM_MODEL_API_KEY", None)
        try:
            model = LLMDrivenDecisionModel()
            self.assertFalse(model.online_ready)
            diag = model.diagnostics()
            self.assertIn("online_ready", diag)
            self.assertFalse(diag["online_ready"])
            self.assertNotIn("api_key", diag)
            self.assertNotIn("CROWDSIM_MODEL_API_KEY", diag)
        finally:
            if old is not None:
                os.environ["CROWDSIM_MODEL_API_KEY"] = old

    def test_ready_only_checks_key_presence_and_value_stays_out_of_diagnostics(self):
        model = LLMDrivenDecisionModel()
        model.api_key = "qa-secret-value"
        self.assertTrue(model.online_ready)
        diag = model.diagnostics()
        self.assertTrue(diag["online_ready"])
        dumped = json.dumps(diag, ensure_ascii=False)
        self.assertNotIn("qa-secret-value", dumped)
        self.assertNotIn("api_key", dumped)

    def test_catalog_marks_llm_offline_with_connector(self):
        llm = entries("decision").get(("decision", "LLM-driven"))
        self.assertIsNotNone(llm)
        self.assertTrue(llm["implemented"])
        self.assertFalse(llm["online_ready"])
        self.assertEqual(llm["connector"], "httpx_openai_compatible")


if __name__ == "__main__":
    unittest.main()
