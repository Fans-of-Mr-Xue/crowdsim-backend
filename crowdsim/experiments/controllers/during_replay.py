"""Replay an already reviewed LLM proposal; never call a model during a run."""
from copy import deepcopy
from .base import BaseController


class DuringReplayController(BaseController):
    controller_id = "during_fixed_replay"

    def reset(self, context=None):
        super().reset(context)
        self.sent = set()

    def due(self, time):
        return any(i not in self.sent and a["startSeconds"] <= time
                   for i, a in enumerate(self.config["strategy"]["actions"]))

    def should_decide(self, observation, context=None):
        return self.due(observation["simTimeSeconds"])

    def decide(self, observation, context=None):
        now = observation["simTimeSeconds"]
        actions = []
        schedule = []
        for i, a in enumerate(self.config["strategy"]["actions"]):
            if i in self.sent or a["startSeconds"] > now:
                continue
            self.sent.add(i)
            schedule.append({**deepcopy(a), "index": i, "dispatchAtSeconds": now})
            actions.append({"actionType": "set_inflow_rate", "target": {"entryId": a["entryId"]},
                            "parameters": {"rate": a["rate"], "durationSeconds": a["endSeconds"] - a["startSeconds"],
                                           "endTimeSeconds": a["endSeconds"]}, "priority": 80})
        decision = self._decision(observation, actions, self.config["strategy"]["rationale"], trigger="confirmed_schedule",
                                  algorithm_trace={"strategyFingerprint": self.config["strategyFingerprint"], "schedule": schedule})
        decision["validUntil"] = max(a["endSeconds"] for a in schedule)
        decision["expectedEffect"] = {"hypothesis": self.config["strategy"]["expectedEffect"], "validated": False}
        return decision

    def snapshot_state(self):
        return {**super().snapshot_state(), "sentIndices": sorted(self.sent),
                "strategyFingerprint": self.config["strategyFingerprint"]}
