"""Explicit information interventions; unsupported physics is rejected."""

from __future__ import annotations

from typing import Any, Dict

from crowdsim.environment.information_model import InformationMessage, InformationModel


class InterventionExecutor:
    SUPPORTED = {"police_guidance", "temporary_diversion", "observe_only"}

    def __init__(self, information: InformationModel) -> None:
        self.information = information
        self.active_policy = ""
        self.records = []

    def apply_command(self, name: str, data: Dict[str, Any], now: float) -> dict:
        if name not in self.SUPPORTED:
            raise ValueError(f"unsupported intervention: {name}")
        record = {"name": name, "applied_at": now, "physical_change": False}
        if name != "observe_only":
            message_id = str(data.get("id") or f"policy-{name}-{now:g}")
            self.information.publish(InformationMessage(message_id=message_id, content=str(data.get("message") or name), source_type="official", created_at=now, deliver_at=now, expires_at=now + max(1.0, float(data.get("duration", 60))), x=data.get("x"), y=data.get("y"), radius=data.get("radius"), event_id=data.get("event_id")))
            record["message_id"] = message_id
        self.active_policy = name
        self.records.append(record)
        return record
