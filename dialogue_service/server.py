"""Local HTTP API for CrowdSim conversation storage."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import socket
import re
from urllib.parse import urlsplit, parse_qs

from dialog_storage.repository import ConversationRepository
from dialog_storage.during_requirements import DuringRequirementRepository, RequirementConflict
from dialog_storage.during_artifacts import DuringArtifactRepository, ArtifactConflict
from dialog_storage.during_empty_conversations import delete_empty_during_conversation
from dialogue_service.logging_setup import configure_logging


MAX_BODY_BYTES = 16 * 1024 * 1024
logger = logging.getLogger(__name__)


def _allowed_origins() -> set[str]:
    hosts = {"localhost", "127.0.0.1"}
    try:
        hosts.update(address for address in socket.gethostbyname_ex(socket.gethostname())[2] if address)
    except OSError:
        pass
    origins = {f"http://{host}:{port}" for host in hosts for port in (8080, 8081, 9090)}
    origins.update(value.strip().rstrip("/") for value in os.environ.get("CROWDSIM_DIALOG_ALLOWED_ORIGINS", "").split(",") if value.strip())
    return origins


class DialogueHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, DialogueHandler)
        self.repository = ConversationRepository()
        self.workbench_repository = ConversationRepository(self.repository.root / "workbench")
        self.during_requirements = DuringRequirementRepository(self.repository.root / "during-requirements")
        self.during_artifacts = DuringArtifactRepository(self.repository.root / "during-artifacts", self.during_requirements)
        self.repository.root.mkdir(parents=True, exist_ok=True)
        self.workbench_repository.root.mkdir(parents=True, exist_ok=True)
        self.allowed_origins = _allowed_origins()


class DialogueHandler(BaseHTTPRequestHandler):
    server: DialogueHttpServer

    def log_message(self, format_string: str, *args) -> None:
        logger.info("HTTP %s %s: %s", self.command, urlsplit(self.path).path, format_string % args)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        logger.info("HTTP %s %s -> %s", self.command, urlsplit(self.path).path, code)

    def _headers(self, status: int, *, length: int = 0) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.headers.get("Origin", "")
        if origin in self.server.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def _send(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, length=len(encoded))
        self.wfile.write(encoded)

    def _permitted(self) -> bool:
        host = self.headers.get("Host", "").lower()
        if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
            self._send(403, {"message": "只允许本机访问对话服务"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.allowed_origins:
            self._send(403, {"message": "不允许的前端来源"})
            return False
        return True

    def _json_body(self) -> dict:
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            raise ValueError("请求必须使用 application/json")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("无效的请求长度") from exc
        if size <= 0 or size > MAX_BODY_BYTES:
            raise ValueError("请求内容为空或超过 16 MB")
        payload = json.loads(self.rfile.read(size))
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return payload

    def do_OPTIONS(self) -> None:
        if self._permitted():
            self._headers(204)

    def do_GET(self) -> None:
        if not self._permitted():
            return
        split = urlsplit(self.path)
        path = split.path
        query = parse_qs(split.query)

        def _int_arg(name: str) -> int | None:
            raw = query.get(name, [None])[0]
            if raw in (None, ""):
                return None
            try:
                return int(raw)
            except ValueError:
                raise ValueError(f"无效的分页参数 {name}")

        try:
            artifacts = re.fullmatch(r"/during/conversations/([A-Za-z0-9_-]{1,120})/artifacts", path)
            if artifacts:
                try:
                    return self._send(200, {"record": self.server.during_artifacts.load(artifacts.group(1), query.get('snapshotId', [None])[0])})
                except (OSError, ValueError):
                    return self._send(503, {"message": "事中派生档案无法读取，原记录未覆盖"})
            match = re.fullmatch(r"/during/conversations/([A-Za-z0-9_-]{1,120})/requirements", path)
            if match:
                try:
                    return self._send(200, {"record": self.server.during_requirements.load(match.group(1))})
                except (OSError, ValueError):
                    return self._send(503, {"message": "事中需求档案无法读取，未创建空白档案"})
            if path == "/health":
                self._send(200, {"service": "crowdsim-dialogue-storage"})
            elif path == "/conversations":
                limit = _int_arg("limit")
                # cursor accepts BOTH the legacy numeric cursor (old clients) and the
                # new opaque `v2:<ts>:<id>` cursor; validation happens in the repository
                # so a garbage cursor surfaces as a strict 400. Never route it through
                # _int_arg: that would 400 every legitimate opaque cursor.
                cursor_raw = query.get("cursor", [None])[0]
                cursor = None if cursor_raw in (None, "") else cursor_raw
                if limit is not None or cursor is not None:
                    self._send(200, self.server.repository.list_meta(limit=limit, cursor=cursor))
                else:
                    self._send(200, {"conversations": self.server.repository.list_all()})
            elif path == "/workbench/conversations":
                self._send(200, {"conversations": self.server.workbench_repository.list_all()})
            else:
                # GET /conversations/{id} — cold by-id read of a single conversation record
                # (meta + messages). Strict id validation; never a full-list dump.
                by_id = re.fullmatch(r"/conversations/([A-Za-z0-9_-]{1,120})", path)
                if by_id:
                    record = self.server.repository.get_conversation(by_id.group(1))
                    if record is None:
                        self._send(404, {"message": "会话不存在"})
                    else:
                        self._send(200, record)
                    return
                match = re.fullmatch(r"/conversations/([A-Za-z0-9_-]{1,120})/messages", path)
                if match:
                    # before accepts legacy numeric (old clients) AND opaque v2:<ts>:<id>
                    # cursor; pass raw through, repository validates. Never _int_arg here.
                    before_raw = query.get("before", [None])[0]
                    before = None if before_raw in (None, "") else before_raw
                    limit = _int_arg("limit")
                    self._send(200, self.server.repository.list_messages(match.group(1), before=before, limit=limit))
                else:
                    self._send(404, {"message": "未找到接口"})
        except ValueError as exc:
            self._send(400, {"message": str(exc)})

    def do_POST(self) -> None:
        if not self._permitted():
            return
        path = urlsplit(self.path).path
        try:
            artifacts = re.fullmatch(r"/during/conversations/([A-Za-z0-9_-]{1,120})/artifacts", path)
            if artifacts:
                try:
                    return self._send(200, {"record": self.server.during_artifacts.save(artifacts.group(1), self._json_body())})
                except ArtifactConflict as exc:
                    return self._send(409, {"message": str(exc)})
            cleanup = re.fullmatch(r"/during/conversations/([A-Za-z0-9_-]{1,120})/delete-empty", path)
            if cleanup:
                payload = self._json_body()
                if "conversation" not in payload or "requirement" not in payload:
                    raise ValueError("missing during cleanup inspection")
                deleted = delete_empty_during_conversation(self.server.repository, self.server.during_requirements,
                    cleanup.group(1), conversation=payload["conversation"], requirement=payload["requirement"])
                return self._send(200 if deleted else 409, {"deleted": deleted})
            match = re.fullmatch(r"/during/conversations/([A-Za-z0-9_-]{1,120})/requirements", path)
            if match:
                payload = self._json_body()
                record = self.server.during_requirements.save(match.group(1), state=payload.get("state"),
                    expected_revision=payload.get("expectedRevision"), mutation_id=payload.get("mutationId"))
                self._send(200, {"record": record})
            elif path == "/conversations/sync":
                payload = self._json_body()
                count = self.server.repository.replace_all(payload.get("conversations"))
                self._send(200, {"saved": count})
            elif path == "/conversations/upsert":
                payload = self._json_body()
                self.server.repository.upsert(payload.get("conversation"))
                self._send(200, {"saved": 1})
            elif path == "/workbench/conversations/sync":
                payload = self._json_body()
                self.server.workbench_repository.upsert(payload.get("conversation"))
                self._send(200, {"saved": 1})
            elif path == "/post-review/chat":
                from dialogue_service.post_agent import respond
                self._send(200, respond(self._json_body()))
            else:
                match = re.fullmatch(r"/conversations/([A-Za-z0-9_-]{1,120})/messages", path)
                if match:
                    payload = self._json_body()
                    result = self.server.repository.append_messages(
                        match.group(1), payload.get("messages") or [], payload.get("meta"),
                        mutation_id=payload.get("mutationId"))
                    self._send(200, result)
                else:
                    self._send(404, {"message": "未找到接口"})
        except RequirementConflict as exc:
            self._send(409, {"code": "DURING_REQUIREMENT_CONFLICT", "message": str(exc), "record": exc.record})
        except ValueError as exc:
            logger.warning("Invalid dialogue request %s: %s", path, exc)
            self._send(400, {"message": str(exc)})
        except OSError as exc:
            logger.exception("Dialogue request failed with OS error: %s", path)
            self._send(503, {"message": f"本机对话存储不可用：{exc}"})
        except Exception as exc:
            logger.exception("Dialogue request failed: %s", path)
            if isinstance(exc, RuntimeError):
                self._send(502, {"message": str(exc)})
            else:
                self._send(502, {"message": f"本机对话存储失败：{type(exc).__name__}"})

    def do_DELETE(self) -> None:
        if not self._permitted():
            return
        path = urlsplit(self.path).path
        try:
            match = re.fullmatch(r"/conversations/([A-Za-z0-9_-]{1,120})", path)
            if match:
                existed = self.server.repository.delete(match.group(1))
                self._send(200, {"deleted": existed})
            else:
                self._send(404, {"message": "未找到接口"})
        except ValueError as exc:
            self._send(400, {"message": str(exc)})
        except OSError as exc:
            self._send(503, {"message": f"本机对话存储不可用：{exc}"})


def main() -> None:
    parser = argparse.ArgumentParser(description="CrowdSim local conversation storage service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    log_path = configure_logging()
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error("对话服务只能监听本机回环地址")
    server = DialogueHttpServer((args.host, args.port))
    logger.info("CrowdSim conversation storage listening on http://%s:%s; log=%s",
                args.host, args.port, log_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
