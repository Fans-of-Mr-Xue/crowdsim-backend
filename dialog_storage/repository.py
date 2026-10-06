"""Atomic JSON snapshots of the conversations shown in the frontend."""

from __future__ import annotations

import json
from pathlib import Path
import re
import threading


CONVERSATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,120}$")


class ConversationRepository:
    def __init__(self, root: Path | None = None) -> None:
        # runs/ is already excluded from the Crowdbackend repository.
        self.root = root or Path(__file__).resolve().parents[1] / "runs" / "dialogues"
        self._lock = threading.RLock()

    def list_all(self) -> list[dict]:
        with self._lock:
            if not self.root.exists():
                return []
            conversations = []
            for path in self.root.glob("*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(record, dict) and record.get("id") == path.stem:
                    conversations.append(record)
            return sorted(conversations, key=lambda item: item.get("updatedAt") or 0, reverse=True)

    def replace_all(self, conversations: list[dict]) -> int:
        if not isinstance(conversations, list) or len(conversations) > 500:
            raise ValueError("conversations must be an array of at most 500 items")
        normalized: dict[str, dict] = {}
        for conversation in conversations:
            if not isinstance(conversation, dict):
                raise ValueError("each conversation must be an object")
            conversation_id = conversation.get("id")
            if not isinstance(conversation_id, str) or not CONVERSATION_ID.fullmatch(conversation_id):
                raise ValueError("invalid conversation id")
            messages = conversation.get("messages")
            if not isinstance(messages, list) or len(messages) > 2000:
                raise ValueError("conversation messages must be an array of at most 2000 items")
            for message in messages:
                if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"}:
                    raise ValueError("invalid conversation message")
            normalized[conversation_id] = conversation

        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            for conversation_id, conversation in normalized.items():
                target = self.root / f"{conversation_id}.json"
                temporary = self.root / f"{conversation_id}.json.tmp"
                temporary.write_text(json.dumps(conversation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                temporary.replace(target)
            for path in self.root.glob("*.json"):
                if path.stem not in normalized:
                    path.unlink()
        return len(normalized)

    def upsert(self, conversation: dict) -> None:
        if not isinstance(conversation, dict):
            raise ValueError("conversation must be an object")
        conversation_id = conversation.get("id")
        if not isinstance(conversation_id, str) or not CONVERSATION_ID.fullmatch(conversation_id):
            raise ValueError("invalid conversation id")
        messages = conversation.get("messages")
        if not isinstance(messages, list) or len(messages) > 2000:
            raise ValueError("invalid conversation messages")
        if any(not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"} for message in messages):
            raise ValueError("invalid conversation message")
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            target = self.root / f"{conversation_id}.json"
            temporary = self.root / f"{conversation_id}.json.tmp"
            temporary.write_text(json.dumps(conversation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(target)
