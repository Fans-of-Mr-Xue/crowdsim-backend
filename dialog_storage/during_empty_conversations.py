"""Guarded deletion for unused during conversations; other phases are untouched."""
from __future__ import annotations

from .repository import CONVERSATION_ID


def _empty(value):
    return value is None or isinstance(value, (dict, list)) and not value


def _empty_tree(value):
    return _empty(value) or isinstance(value, dict) and all(_empty_tree(v) for v in value.values())


def only_welcome(conversation):
    if not isinstance(conversation, dict):
        return False
    if (conversation.get("displayPhase") or conversation.get("mode")) != "during":
        return False
    messages = conversation.get("messages")
    return isinstance(messages, list) and all(
        isinstance(m, dict) and m.get("role") == "assistant" and m.get("welcomeIntro") is True
        and not m.get("files") and not m.get("attachments") and not m.get("duringToolTrace")
        and m.get("status") not in {"loading", "error"}
        for m in messages
    )


def pristine_requirement(record):
    if record is None:
        return True
    state = record.get("state")
    if not isinstance(state, dict) or set(state) - {"requirement", "observation", "legacy"} or state.get("legacy") is not None:
        return False
    observation = state.get("observation")
    if observation is not None and (not isinstance(observation, dict) or any(
        not (_empty(v) if k in {"entries", "preparedModels"} else k == "selectedId" and not v)
        for k, v in observation.items()
    )):
        return False
    requirement = state.get("requirement")
    if not isinstance(requirement, dict) or requirement.get("version") != 3 or requirement.get("view") not in {None, "requirement"}:
        return False
    draft = requirement.get("draft")
    if not isinstance(draft, dict) or type(draft.get("revision")) is not int or draft["revision"] != 0:
        return False
    if any(not _empty(v) for k, v in draft.items() if k not in {"id", "revision", "requirement", "migration"}):
        return False
    if set(draft.get("migration") or {}) - {"metricSelectionVersion", "populationCatalogVersion"}:
        return False
    values = draft.get("requirement")
    if not isinstance(values, dict) or values.get("schema_version") != 2:
        return False
    for key, value in values.items():
        if key == "schema_version":
            continue
        if key == "observation" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k != "selection_schema_version"}
        if not _empty_tree(value):
            return False
    allowed = {"version", "view", "draft", "modelingRequirements", "artificialSocietyDraft"}
    return all(k in allowed or _empty(v) for k, v in requirement.items())


def delete_empty_during_conversation(repository, requirements, conversation_id, *, conversation, requirement):
    if not isinstance(conversation_id, str) or not CONVERSATION_ID.fullmatch(conversation_id):
        raise ValueError("invalid during conversation id")
    # Same locks as message/requirement writes: a reply, edit or confirmation
    # arriving after the client's inspection makes this comparison fail closed.
    with repository._lock, requirements._lock:
        current = repository.get_conversation(conversation_id)
        current_requirement = requirements.load(conversation_id)
        if current is None and (repository.root / f"{conversation_id}.json").exists():
            return False  # A corrupt/unreadable file is not an absent conversation.
        if current != conversation or current_requirement != requirement:
            return False
        if current is not None and not only_welcome(current):
            return False
        if not pristine_requirement(current_requirement):
            return False
        if current is not None:
            repository.delete(conversation_id)
        # Requirement/model/experiment records are never deleted here.
        return True
