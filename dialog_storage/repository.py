"""Atomic JSON snapshots of the conversations shown in the frontend.

On-disk layout (back-compatible with the original whole-conversation files):
  runs/dialogues/<conversation_id>.json          conversation record (messages WITHOUT file.data)
  runs/dialogues/_attachments/<message_id>.json  attachment blob {messageId, conversationId, files:[{name,type,data}]}

Meta reads are cached by (mtime, size) so the sidebar paging endpoint never has
to re-read every conversation's message body on every request.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import threading


CONVERSATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
MESSAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
# Opaque versioned cursor: `v2:<updatedAt>:<id>`. Conversation ids never contain
# ':' (see CONVERSATION_ID), so a 3-part split is unambiguous.
_OPAQUE_CURSOR = re.compile(r"^v2:(\d+):([A-Za-z0-9_-]{1,120})$")


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _row_key(item: dict) -> tuple[int, str]:
    return (int(item.get("updatedAt") or 0), str(item.get("id") or ""))


def _row_key_msg(m: dict) -> tuple[int, str]:
    return (int(_as_int(m.get("timestamp"))), str(m.get("id") or ""))


def _encode_cursor(item: dict) -> str:
    ts, conv_id = _row_key(item)
    return f"v2:{ts}:{conv_id}"


def _decode_cursor(cursor):
    """Normalize a list_meta cursor.

    Returns (key, legacy_numeric) where key is the exclusive (updatedAt, id)
    tuple to page from. Legacy numeric cursors (old clients) page on timestamp
    only; opaque `v2:` cursors page on the full tuple. Anything else is rejected.
    """
    if cursor is None:
        return None, False
    if isinstance(cursor, (int, float)) and not isinstance(cursor, bool):
        return (int(cursor), None), True
    text = str(cursor)
    if text.isdigit():  # legacy numeric cursor arriving as a query string
        return (int(text), None), True
    match = _OPAQUE_CURSOR.match(text)
    if match:
        return (int(match.group(1)), match.group(2)), False
    raise ValueError("无效的分页游标")


class ConversationRepository:
    def __init__(self, root: Path | None = None) -> None:
        # runs/ is already excluded from the Crowdbackend repository.
        self.root = root or Path(__file__).resolve().parents[1] / "runs" / "dialogues"
        self._lock = threading.RLock()
        self._meta_cache: dict[str, tuple[int, int, dict]] = {}  # id -> (mtime_ns, size, meta)

    @property
    def attachments_dir(self) -> Path:
        return self.root / "_attachments"

    # --- attachment sidecar helpers -------------------------------------------

    def _extract_attachments(self, conversation: dict) -> dict:
        """Move file.data out of the record into sidecar files; return a slimmed copy."""
        conv_id = conversation.get("id")
        slim = json.loads(json.dumps(conversation, ensure_ascii=False))
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        for message in slim.get("messages") or []:
            files = message.get("files")
            if not isinstance(files, list):
                continue
            has_data = any(isinstance(f, dict) and f.get("data") for f in files)
            if not has_data:
                continue
            message_id = message.get("id")
            if not isinstance(message_id, str) or not MESSAGE_ID.fullmatch(message_id):
                continue
            sidecar = {"messageId": message_id, "conversationId": conv_id, "files": files}
            target = self.attachments_dir / f"{message_id}.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(sidecar, ensure_ascii=False) + "\n", encoding="utf-8")
            temporary.replace(target)
            # Strip data from the slim record; keep name/type for rendering placeholders.
            message["files"] = [{k: v for k, v in f.items() if k != "data"} if isinstance(f, dict) else f for f in files]
        return slim

    def _restore_attachments(self, record: dict) -> dict:
        out = json.loads(json.dumps(record, ensure_ascii=False))
        if not self.attachments_dir.exists():
            return out
        for message in out.get("messages") or []:
            files = message.get("files")
            if not isinstance(files, list):
                continue
            if any(isinstance(f, dict) and f.get("data") for f in files):
                continue  # already inline (legacy record)
            message_id = message.get("id")
            if not isinstance(message_id, str):
                continue
            sidecar = self.attachments_dir / f"{message_id}.json"
            if not sidecar.exists():
                continue
            try:
                blob = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            message["files"] = blob.get("files") or files
        return out

    # --- reads -----------------------------------------------------------------

    def _read_full_record(self, path: Path) -> dict | None:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not (isinstance(record, dict) and record.get("id") == path.stem):
            return None
        return self._restore_attachments(record)

    def list_all(self) -> list[dict]:
        with self._lock:
            if not self.root.exists():
                return []
            conversations = []
            for path in self.root.glob("*.json"):
                record = self._read_full_record(path)
                if record is not None:
                    conversations.append(record)
            return sorted(conversations, key=lambda item: item.get("updatedAt") or 0, reverse=True)

    @staticmethod
    def _meta(record: dict) -> dict:
        meta = {key: value for key, value in record.items() if key != "messages"}
        messages = record.get("messages")
        meta["messageCount"] = len(messages) if isinstance(messages, list) else 0
        return meta

    def list_meta(self, limit: int | None = None, cursor=None) -> dict:
        # Page metadata without re-reading message bodies when the file is unchanged.
        # Rows sort by the STABLE (updatedAt, id) tuple so a page boundary never
        # lands on two conversations sharing one updatedAt (the old numeric cursor
        # dropped every row that shared the cursor timestamp).
        with self._lock:
            if not self.root.exists():
                return {"conversations": [], "nextCursor": None, "hasMore": False}
            rows: list[dict] = []
            for path in self.root.glob("*.json"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                cached = self._meta_cache.get(path.stem)
                if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
                    rows.append(cached[2])
                    continue
                record = self._read_full_record(path)
                if record is None:
                    continue
                meta = self._meta(record)
                self._meta_cache[path.stem] = (stat.st_mtime_ns, stat.st_size, meta)
                rows.append(meta)
            rows.sort(key=_row_key, reverse=True)
        decoded, legacy = _decode_cursor(cursor)
        if decoded is not None:
            if legacy or decoded[1] is None:
                # Legacy numeric cursor: exclusive timestamp boundary (old contract).
                rows = [item for item in rows if _row_key(item)[0] < decoded[0]]
            else:
                rows = [item for item in rows if _row_key(item) < decoded]
        page = rows[:limit] if limit is not None else rows
        next_cursor = _encode_cursor(page[-1]) if page else None
        return {"conversations": page, "nextCursor": next_cursor,
                "hasMore": bool(limit is not None and len(rows) > len(page))}

    def get_conversation(self, conversation_id: str) -> dict | None:
        if not isinstance(conversation_id, str) or not CONVERSATION_ID.fullmatch(conversation_id):
            raise ValueError("invalid conversation id")
        path = self.root / f"{conversation_id}.json"
        if not path.exists():
            return None
        with self._lock:
            return self._read_full_record(path)

    def list_messages(self, conversation_id: str, before=None, limit: int | None = None) -> dict:
        record = self.get_conversation(conversation_id)
        if record is None:
            return {"messages": [], "hasMore": False, "nextBefore": None}
        messages = record.get("messages")
        if not isinstance(messages, list):
            return {"messages": [], "hasMore": False, "nextBefore": None}
        # Stable order: (timestamp, id) ascending (record/insertion order = oldest-first),
        # then take the newest N rows (tail slice). This preserves the old tail-slice
        # contract while making the page boundary not split a same-timestamp cluster.
        ordered = sorted(
            messages,
            key=lambda m: (int(_as_int(m.get("timestamp"))), str(m.get("id") or "")),
        )
        decoded, legacy = _decode_cursor(before)
        if decoded is not None:
            if legacy or decoded[1] is None:
                ordered = [m for m in ordered if _row_key_msg(m)[0] < decoded[0]]
            else:
                ordered = [m for m in ordered if _row_key_msg(m) < decoded]
        page = ordered[-limit:] if limit is not None else ordered
        next_before = f"v2:{_row_key_msg(page[0])[0]}:{_row_key_msg(page[0])[1]}" if page else None
        return {"messages": page, "hasMore": bool(limit is not None and len(ordered) > len(page)),
                "nextBefore": next_before}

    # --- writes ------------------------------------------------------------------

    def _atomic_write(self, conversation_id: str, slim: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{conversation_id}.json"
        temporary = self.root / f"{conversation_id}.json.tmp"
        temporary.write_text(json.dumps(slim, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
        self._meta_cache.pop(conversation_id, None)

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
            for conversation_id, conversation in normalized.items():
                self._atomic_write(conversation_id, self._extract_attachments(conversation))
            for path in self.root.glob("*.json"):
                if path.stem not in normalized:
                    path.unlink()
                    self._meta_cache.pop(path.stem, None)
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
            self._atomic_write(conversation_id, self._extract_attachments(conversation))

    def append_messages(self, conversation_id: str, messages: list, meta_patch: dict | None = None, mutation_id: str | None = None) -> dict:
        # Non-destructive merge: add/replace messages by id, never drop existing ones
        # the caller did not send. This protects against a paged/evicted client view
        # overwriting the server-side history with only the messages it has in memory.
        if not isinstance(conversation_id, str) or not CONVERSATION_ID.fullmatch(conversation_id):
            raise ValueError("invalid conversation id")
        if not isinstance(messages, list) or len(messages) > 2000:
            raise ValueError("messages must be an array of at most 2000 items")
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"}:
                raise ValueError("invalid conversation message")
            if not isinstance(message.get("id"), str):
                raise ValueError("message id required")
        sent_ids = [m["id"] for m in messages]
        with self._lock:
            existing = self.get_conversation(conversation_id) or {"id": conversation_id, "messages": []}
            by_id = {m.get("id"): m for m in existing.get("messages") if isinstance(m, dict)}
            for m in messages:
                by_id[m["id"]] = m
            merged_messages = sorted(by_id.values(), key=lambda m: _as_int(m.get("timestamp")))
            updated = {**existing, **(meta_patch or {}), "messages": merged_messages}
            self._atomic_write(conversation_id, self._extract_attachments(updated))
            return {"id": conversation_id, "saved": True, "ackedMessageIds": sent_ids,
                    "messageCount": len(merged_messages), "mutationId": mutation_id}

    def delete(self, conversation_id: str) -> bool:
        # Explicit single-conversation delete. Never prunes other records.
        if not isinstance(conversation_id, str) or not CONVERSATION_ID.fullmatch(conversation_id):
            raise ValueError("invalid conversation id")
        with self._lock:
            target = self.root / f"{conversation_id}.json"
            if not target.exists():
                return False
            target.unlink()
            self._meta_cache.pop(conversation_id, None)
            # Remove sidecar attachments for this conversation.
            if self.attachments_dir.exists():
                for sidecar in self.attachments_dir.glob("*.json"):
                    try:
                        blob = json.loads(sidecar.read_text(encoding="utf-8"))
                        if blob.get("conversationId") == conversation_id:
                            sidecar.unlink()
                    except (OSError, ValueError):
                        continue
            return True
