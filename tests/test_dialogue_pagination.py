"""Isolated tests for conversation metadata paging, cursor message paging,
and attachment sidecar storage. Uses a temp dir; never touches user data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dialog_storage.repository import ConversationRepository


def _conv(cid: str, updated: int, n_messages: int = 0, with_attachment: bool = False) -> dict:
    messages = []
    for i in range(n_messages):
        msg = {"id": f"{cid}-m{i}", "role": "user", "content": f"msg {i}",
               "timestamp": updated - (n_messages - i), "status": "sent"}
        if with_attachment and i == 0:
            msg["files"] = [{"name": "a.png", "type": "image/png", "data": "BASE64DATA==="}]
        messages.append(msg)
    return {"id": cid, "title": cid, "mode": "auto", "displayPhase": "auto",
            "updatedAt": updated, "messages": messages}


def test_list_meta_pages_with_cursor_and_caches(tmp_path: Path):
    repo = ConversationRepository(tmp_path)
    for i in range(5):
        repo.upsert(_conv(f"c{i}", updated=1000 - i, n_messages=10))

    page1 = repo.list_meta(limit=2)
    assert [c["id"] for c in page1["conversations"]] == ["c0", "c1"]
    assert page1["hasMore"] is True
    assert "messages" not in page1["conversations"][0]
    assert page1["conversations"][0]["messageCount"] == 10

    page2 = repo.list_meta(limit=2, cursor=page1["nextCursor"])
    assert [c["id"] for c in page2["conversations"]] == ["c2", "c3"]

    # Cache hit: modify nothing, stats identical -> second call returns same.
    again = repo.list_meta(limit=2)
    assert [c["id"] for c in again["conversations"]] == ["c0", "c1"]


def test_list_messages_cursor_window(tmp_path: Path):
    repo = ConversationRepository(tmp_path)
    repo.upsert(_conv("c1", updated=100, n_messages=10))

    page = repo.list_messages("c1", limit=4)
    # Newest 4, oldest-first order preserved in record.
    assert [m["id"] for m in page["messages"]] == ["c1-m6", "c1-m7", "c1-m8", "c1-m9"]
    assert page["hasMore"] is True

    older = repo.list_messages("c1", before=page["nextBefore"], limit=4)
    assert [m["id"] for m in older["messages"]] == ["c1-m2", "c1-m3", "c1-m4", "c1-m5"]


def test_attachment_data_split_to_sidecar_and_restored(tmp_path: Path):
    repo = ConversationRepository(tmp_path)
    repo.upsert(_conv("c-att", updated=5, n_messages=1, with_attachment=True))

    # On-disk conversation record must NOT contain base64 data.
    on_disk = json.loads((tmp_path / "c-att.json").read_text(encoding="utf-8"))
    assert on_disk["messages"][0]["files"][0] == {"name": "a.png", "type": "image/png"}

    # Sidecar file holds the bytes.
    sidecar = json.loads((tmp_path / "_attachments" / "c-att-m0.json").read_text(encoding="utf-8"))
    assert sidecar["files"][0]["data"] == "BASE64DATA==="

    # Read-back restores attachment.
    loaded = repo.get_conversation("c-att")
    assert loaded["messages"][0]["files"][0]["data"] == "BASE64DATA==="


def test_legacy_record_with_inline_attachment_still_reads(tmp_path: Path):
    # Old-format record (attachment inline, no sidecar) must remain readable.
    legacy = _conv("c-old", updated=1, n_messages=1, with_attachment=True)
    (tmp_path / "c-old.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    repo = ConversationRepository(tmp_path)
    loaded = repo.get_conversation("c-old")
    assert loaded["messages"][0]["files"][0]["data"] == "BASE64DATA==="


def test_replace_all_prunes_but_keeps_attachments_for_remaining(tmp_path: Path):
    repo = ConversationRepository(tmp_path)
    repo.upsert(_conv("keep", updated=5, n_messages=1, with_attachment=True))
    repo.upsert(_conv("drop", updated=4, n_messages=1))
    repo.replace_all([_conv("keep", updated=6, n_messages=1, with_attachment=True)])
    assert not (tmp_path / "drop.json").exists()
    loaded = repo.get_conversation("keep")
    assert loaded["messages"][0]["files"][0]["data"] == "BASE64DATA==="


def test_append_messages_merges_without_dropping_older_ones(tmp_path: Path):
    repo = ConversationRepository(tmp_path)
    repo.upsert({"id": "c-merge", "messages": [
        {"id": "m0", "role": "user", "timestamp": 1, "status": "sent"},
        {"id": "m1", "role": "user", "timestamp": 2, "status": "sent"},
        {"id": "m2", "role": "user", "timestamp": 3, "status": "sent"},
    ]})
    result = repo.append_messages("c-merge", [
        {"id": "m2", "role": "user", "content": "updated", "timestamp": 3, "status": "sent"},
        {"id": "m3", "role": "assistant", "content": "new", "timestamp": 4, "status": "sent"},
    ], meta_patch={"title": "renamed"})
    assert result["messageCount"] == 4
    loaded = repo.get_conversation("c-merge")
    assert [m["id"] for m in loaded["messages"]] == ["m0", "m1", "m2", "m3"]
    assert loaded["title"] == "renamed"


def test_delete_one_conversation_does_not_prune_others(tmp_path: Path):
    repo = ConversationRepository(tmp_path)
    repo.upsert(_conv("keep1", updated=5, n_messages=1))
    repo.upsert(_conv("del1", updated=4, n_messages=1))
    assert repo.delete("del1") is True
    assert not (tmp_path / "del1.json").exists()
    assert (tmp_path / "keep1.json").exists()
    assert repo.get_conversation("keep1") is not None
