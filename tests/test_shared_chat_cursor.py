"""Shared-chat cursor contract: opaque (updatedAt, id) versioned cursor.

Reproduces the production gap: with >=100 conversations sharing one updatedAt,
the old numeric cursor filtered strictly `updatedAt < cursor`, so every
conversation sharing the cursor timestamp was dropped on page 2 (data loss),
while reusing a row's timestamp as cursor could also re-fetch already-seen
rows. The new contract pages on a stable (updatedAt, id) tuple encoded as an
opaque `v2:<ts>:<id>` string, keeps accepting legacy numeric cursors, and
strictly rejects garbage cursors with a 400.

Runs against a temp dir + a real HTTP server on an OS-ephemeral port.
Never touches user runs/dialogues data.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from dialog_storage.repository import ConversationRepository


def _conv(cid: str, updated: int) -> dict:
    return {"id": cid, "title": cid, "mode": "auto", "displayPhase": "auto",
            "updatedAt": updated, "createdAt": updated, "messages": []}


# --- repository unit level ---------------------------------------------------

def test_same_timestamp_120_pages_without_miss_or_dupe(tmp_path: Path):
    repo = ConversationRepository(tmp_path)
    n = 120
    for i in range(n):
        repo.upsert(_conv(f"c{i:03d}", updated=1000))  # ALL share one timestamp

    seen: list[str] = []
    cursor = None
    for _ in range(30):
        page = repo.list_meta(limit=20, cursor=cursor)
        seen.extend(c["id"] for c in page["conversations"])
        cursor = page["nextCursor"]
        if not page["hasMore"]:
            break

    assert len(seen) == n, f"missing/extra rows: got {len(seen)} of {n}"
    assert len(set(seen)) == n, "duplicated rows while paging"
    # Stable order: sort is (updatedAt, id) descending, so ids descend within the
    # shared timestamp and every row appears exactly once.
    assert seen == sorted(seen, reverse=True)


def test_legacy_numeric_cursor_still_accepted(tmp_path: Path):
    repo = ConversationRepository(tmp_path)
    for i in range(6):
        repo.upsert(_conv(f"c{i}", updated=1000 - i))

    page1 = repo.list_meta(limit=2, cursor=None)
    assert [c["id"] for c in page1["conversations"]] == ["c0", "c1"]
    # Legacy clients may still hand back a raw numeric timestamp cursor.
    page2 = repo.list_meta(limit=2, cursor=1000 - 1)
    assert [c["id"] for c in page2["conversations"]] == ["c2", "c3"]


def test_opaque_cursor_is_string_and_roundtrips(tmp_path: Path):
    repo = ConversationRepository(tmp_path)
    for i in range(5):
        repo.upsert(_conv(f"c{i}", updated=1000 - i))

    page1 = repo.list_meta(limit=2)
    assert isinstance(page1["nextCursor"], str)
    assert page1["nextCursor"].startswith("v2:")
    page2 = repo.list_meta(limit=2, cursor=page1["nextCursor"])
    assert [c["id"] for c in page2["conversations"]] == ["c2", "c3"]


def test_garbage_cursor_rejected(tmp_path: Path):
    repo = ConversationRepository(tmp_path)
    repo.upsert(_conv("c0", updated=1000))
    with pytest.raises(ValueError):
        repo.list_meta(limit=10, cursor="not-a-cursor!!")
    with pytest.raises(ValueError):
        repo.list_meta(limit=10, cursor="v2:abc:c0")  # ts must be digits


def test_messages_120_page_order_is_stable(tmp_path: Path):
    repo = ConversationRepository(tmp_path)
    msgs = [{"id": f"m{i:03d}", "role": "user", "content": f"x{i}",
             "timestamp": 1000 + i, "status": "sent"} for i in range(120)]
    repo.upsert({"id": "c-msg", "updatedAt": 2000, "messages": msgs})

    seen: list[str] = []
    before = None
    prev_oldest_ts = None
    for _ in range(10):
        page = repo.list_messages("c-msg", before=before, limit=25)
        seen.extend(m["id"] for m in page["messages"])
        if not page["messages"]:
            break
        # Monotonic: each page's newest row must be older than the previous page's
        # oldest row, otherwise rows would repeat or be skipped.
        if prev_oldest_ts is not None:
            assert max(int(m["timestamp"]) for m in page["messages"]) < prev_oldest_ts
        prev_oldest_ts = min(int(m["timestamp"]) for m in page["messages"])
        before = page["nextBefore"]
        if not page["hasMore"]:
            break
    assert len(seen) == 120
    assert len(set(seen)) == 120


# --- real HTTP level ---------------------------------------------------------

def _start_server(root: Path):
    from dialogue_service.server import DialogueHttpServer
    server = DialogueHttpServer(("127.0.0.1", 0))
    server.repository = ConversationRepository(root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _get(server, path: str):
    url = f"http://127.0.0.1:{server.server_port}{path}"
    req = urllib.request.Request(url)  # no Origin header: localhost-origin checks stay quiet
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_http_same_timestamp_120_old_numeric_invalid_cursor(tmp_path: Path):
    root = tmp_path / "dialogues"
    server = _start_server(root)
    try:
        repo = ConversationRepository(root)
        n = 120
        for i in range(n):
            repo.upsert(_conv(f"c{i:03d}", updated=1000))

        # 1) opaque paging over 120 rows sharing one timestamp
        seen: list[str] = []
        cursor = None
        for _ in range(30):
            query = "limit=20" + (f"&cursor={cursor}" if cursor else "")
            status, payload = _get(server, f"/conversations?{query}")
            assert status == 200
            seen.extend(c["id"] for c in payload["conversations"])
            cursor = payload.get("nextCursor")
            if not payload.get("hasMore"):
                break
        assert len(seen) == n and len(set(seen)) == n

        # 2) legacy numeric cursor still served (200, not 400)
        status, payload = _get(server, "/conversations?limit=20&cursor=1000")
        assert status == 200
        assert payload["conversations"] == []  # strictly older than ts==1000: empty

        # 3) garbage cursor strictly rejected
        status, _ = _get(server, "/conversations?limit=20&cursor=!!bad!!")
        assert status == 400
    finally:
        server.server_close()
