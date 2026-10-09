"""One command supervises the existing services and their SSH tunnels."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
from typing import Callable
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent


class StartupError(Exception):
    pass


def report(message):
    print(f"[CrowdSim] {message}", flush=True)


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def get_json(port, path, api_key=""):
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    # Local probes must not be sent to the user's HTTP/SOCKS proxy.
    with build_opener(ProxyHandler({})).open(Request(f"http://127.0.0.1:{port}{path}", headers=headers), timeout=2) as response:
        return json.load(response)


@dataclass
class Service:
    name: str
    port: int
    command: list[str]
    probe: Callable[[], bool]


class Supervisor:
    def __init__(self):
        self.children = []

    def spawn(self, name, command, *, interactive=False):
        report(f"启动 {name}")
        child = subprocess.Popen(command, cwd=ROOT, env={**os.environ, "PYTHONUNBUFFERED": "1"},
                                 stdin=None if interactive else subprocess.DEVNULL,
                                 start_new_session=os.name != "nt")
        self.children.append((name, child))
        return child

    def check_children(self):
        for name, child in self.children:
            if child.poll() is not None:
                raise StartupError(f"{name} 已退出（退出码 {child.returncode}），请检查上方日志")

    def wait_ready(self, name, probe, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_children()
            try:
                if probe():
                    report(f"{name} 已就绪")
                    return
            except Exception:
                pass
            time.sleep(0.3)
        raise StartupError(f"{name} 在 {timeout} 秒内未就绪，请检查端口、认证和远程服务")

    def start_service(self, service):
        if listening(service.port):
            try:
                valid = service.probe()
            except Exception:
                valid = False
            if not valid:
                raise StartupError(f"{service.port} 已被占用，但不是可用的 {service.name}；请检查该端口")
            report(f"复用已有 {service.name}（{service.port}），退出时不会关闭它")
            return
        self.spawn(service.name, service.command)
        self.wait_ready(service.name, service.probe)

    def close(self):
        # Only stop processes created by this launcher, including their descendants.
        for name, child in reversed(self.children):
            report(f"停止 {name}")
            try:
                if os.name == "nt":
                    if child.poll() is None:
                        child.terminate()
                else:
                    os.killpg(child.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        deadline = time.monotonic() + 5
        for _, child in reversed(self.children):
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    child.kill()
                else:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                child.wait()
        self.children.clear()


def tunnel_settings(kind):
    prefix = f"CROWDSIM_{kind}_SSH_"
    defaults = {"HOST": "211.81.55.186", "PORT": "3000", "USER": "hyq"} if kind == "DB" else {}
    values = {key: os.environ.get(prefix + key, defaults.get(key, "")) for key in ("HOST", "PORT", "USER")}
    if not values["HOST"] or not values["USER"]:
        raise StartupError(f"本机模型服务未就绪，请在 .env 配置 {prefix}HOST、{prefix}PORT、{prefix}USER 和 CROWDSIM_MODEL_REMOTE_PORT")
    remote_port = os.environ.get(f"CROWDSIM_{kind}_REMOTE_PORT", "27018" if kind == "DB" else "8800")
    remote_host = os.environ.get(f"CROWDSIM_{kind}_REMOTE_HOST", "127.0.0.1")
    if not 1 <= int(values["PORT"]) <= 65535 or not 1 <= int(remote_port) <= 65535:
        raise StartupError(f"{kind} SSH 配置中的端口无效")
    for value in (values["HOST"], values["USER"], remote_host):
        if value.startswith("-") or any(character.isspace() for character in value):
            raise StartupError(f"{kind} SSH 配置中的地址或用户名无效")
    return values, remote_host, remote_port


def ensure_tunnels(supervisor, *, enabled=True):
    from data_service.repositories.datasets import DatasetRepository
    repository = DatasetRepository()
    try:
        database_probe = lambda: repository.status()["connected"]
        # URI or a directly reachable MongoDB deployment does not need a tunnel.
        try:
            database_ready = database_probe()
        except Exception:
            database_ready = False
        model_key = os.environ.get("CROWDSIM_MODEL_API_KEY", "")
        model_probe = lambda: isinstance(get_json(8800, "/v1/models", model_key).get("data"), list)
        try:
            model_ready = model_probe()
        except Exception:
            model_ready = False
        pending = []
        for kind, port, ready, probe in (("DB", 27018, database_ready, database_probe), ("MODEL", 8800, model_ready, model_probe)):
            if ready:
                report(f"{'数据库' if kind == 'DB' else '模型'}连接已可用，复用现有连接")
                continue
            if not enabled:
                raise StartupError(f"{kind} 连接不可用，且已禁用自动 SSH 隧道")
            if listening(port):
                raise StartupError(f"{port} 已占用但 {kind} 连接不可用，请检查已有隧道或认证配置")
            if kind == "DB" and (os.environ.get("CROWDSIM_MONGO_URI") or os.environ.get("CROWDSIM_MONGO_HOST", "127.0.0.1") != "127.0.0.1" or int(os.environ.get("CROWDSIM_MONGO_PORT", "27018")) != port):
                raise StartupError("自定义 MongoDB 连接不可用，请检查配置；不会为其自动建立默认隧道")
            settings, remote_host, remote_port = tunnel_settings(kind)
            pending.append((kind, port, probe, settings, remote_host, remote_port))
        # Group forwards to the same SSH destination into one connection.
        groups = {}
        for item in pending:
            settings = item[3]
            groups.setdefault((settings["HOST"], settings["PORT"], settings["USER"]), []).append(item)
        for (host, port, user), items in groups.items():
            # Explicit minimal SSH settings avoid unrelated forwards in ~/.ssh/config.
            command = ["ssh", "-F", os.devnull, "-N", "-o", "ExitOnForwardFailure=yes",
                       "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=10", "-p", port]
            identity = os.environ.get("CROWDSIM_SSH_IDENTITY_FILE")
            if identity:
                command.extend(["-i", str(Path(identity).expanduser())])
            for _, local_port, _, _, remote_host, remote_port in items:
                command.extend(["-L", f"127.0.0.1:{local_port}:{remote_host}:{remote_port}"])
            command.append(f"{user}@{host}")
            supervisor.spawn("SSH " + "+".join(item[0] for item in items), command, interactive=True)
            for kind, _, probe, *_ in items:
                supervisor.wait_ready(f"{kind} 隧道", probe)
    finally:
        repository.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="一键启动 CrowdSim 仿真、对话、数据服务及 SSH 隧道")
    parser.add_argument("--scenario", choices=("research", "hotspot"), default="hotspot")
    parser.add_argument("--mode", choices=("rule", "llm"), default="rule")
    parser.add_argument("--sumo-binary")
    parser.add_argument("--no-tunnels", action="store_true", help="复用已有连接，不创建 SSH 隧道")
    parser.add_argument("--check", action="store_true", help="只检查依赖、SUMO 和配置，不启动进程")
    args, simulation_args = parser.parse_known_args(argv)
    supervisor = Supervisor()
    try:
        def request_shutdown(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, request_shutdown)
        if sys.version_info < (3, 10):
            raise StartupError("统一启动需要 Python 3.10 或更新版本，请使用安装了 requirements.txt 的解释器")
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
        os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
        from crowdsim.infrastructure.sumo_adapter import discover_sumo_binary
        from crowdsim_overlay_server import parse_args, resolve_service_scenario
        sumo_args = ["--scenario", args.scenario, "--mode", args.mode, *simulation_args]
        if args.sumo_binary:
            sumo_args += ["--sumo-binary", args.sumo_binary]
        options = parse_args(sumo_args)
        if options.host not in ("127.0.0.1", "localhost") or options.port != 8765:
            raise StartupError("统一入口固定使用本机 8765；自定义仿真监听地址请使用原仿真入口")
        sumo_binary = discover_sumo_binary(args.sumo_binary)
        version = subprocess.run([sumo_binary, "--version"], capture_output=True, text=True, timeout=10)
        if version.returncode != 0:
            raise StartupError("SUMO 程序无法执行（可能缺少系统动态库），请先修复 SUMO 安装或设置 SUMO_BINARY；未启动任何服务")
        if not re.search(r"Version (?:1\.24\.0(?:\s|\+|$)|v1_24_0(?:\s|\+|$))", version.stdout):
            raise StartupError("项目需要 SUMO 1.24.0，请检查 SUMO_BINARY 与 TraCI 版本")
        resolve_service_scenario(options)
        import aiohttp, pymongo  # Verify service dependencies before opening tunnels.
        report("Python、服务依赖和 SUMO 场景检查通过")
        if args.check:
            report("检查结束，未启动进程；模型隧道配置可在 .env 中填写")
            return 0
        ensure_tunnels(supervisor, enabled=not args.no_tunnels)
        def data_ready():
            status = get_json(8768, "/api/crowdSim/database/status").get("data", {})
            return status.get("connected") is True and status.get("database") == os.environ.get("CROWDSIM_MONGO_DATABASE", "crowdsim")

        services = [
            Service("数据服务", 8768, [sys.executable, "-m", "data_service"],
                    data_ready),
            Service("对话存储", 8767, [sys.executable, "-m", "dialogue_service.server"],
                    lambda: get_json(8767, "/health").get("service") == "crowdsim-dialogue-storage"),
            Service("SUMO 服务", 8765, [sys.executable, "crowdsim_overlay_server.py", *sumo_args],
                    lambda: get_json(8765, "/api/v1/post/capabilities").get("data", {}).get("contractVersion") == "post-api/1.0"),
        ]
        for service in services:
            supervisor.start_service(service)
        report("全部就绪：仿真 8765 / 对话 8767 / 数据 8768 / 模型 8800 / MongoDB 27018；Ctrl+C 退出")
        while True:
            supervisor.check_children()
            time.sleep(1)
    except KeyboardInterrupt:
        report("收到退出请求")
        return 0
    except (StartupError, ImportError, FileNotFoundError, ValueError, OSError, subprocess.SubprocessError) as exc:
        report(f"启动失败：{exc}")
        return 1
    finally:
        supervisor.close()


if __name__ == "__main__":
    raise SystemExit(main())
