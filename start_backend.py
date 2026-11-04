"""One command supervises the existing services and their SSH tunnels."""
from __future__ import annotations

import errno
import argparse
from dataclasses import dataclass
import importlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Callable
from urllib.request import ProxyHandler, Request, build_opener
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent
WINDOWS = os.name == "nt"
SUPPORTED_SUMO_VERSIONS = ("1.24.0", "1.27.0")
SERVICE_DEPENDENCIES = (
    ("websockets", "websockets"),
    ("aiohttp", "aiohttp"),
    ("httpx", "httpx"),
    ("traci", "traci"),
    ("sumolib", "sumolib"),
    ("pyproj", "pyproj"),
    ("pymongo", "pymongo"),
    ("python-dotenv", "dotenv"),
    ("pypdf", "pypdf"),
    ("PyMuPDF", "fitz"),
    ("beautifulsoup4", "bs4"),
    ("shapely", "shapely"),
    ("numpy", "numpy"),
    ("socksio", "socksio"),
    ("PyYAML", "yaml"),
)


class StartupError(Exception):
    pass


def report(message):
    print(f"[CrowdSim] {message}", flush=True)


def check_service_dependencies():
    failures = []
    dependencies = SERVICE_DEPENDENCIES + ((("psutil", "psutil"),) if WINDOWS else ())
    for package, module in dependencies:
        try:
            importlib.import_module(module)
        except ImportError as exc:
            failures.append(f"{package}: {exc}")
    if failures:
        raise StartupError(
            "Python 服务依赖未就绪：" + "; ".join(failures)
            + f"；请使用 {sys.executable} -m pip install -r requirements.txt 补齐依赖"
        )


def check_sumo_version(sumo_binary):
    result = subprocess.run([sumo_binary, "--version"], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        raise StartupError(
            f"SUMO 程序无法执行（可能缺少系统动态库）：{sumo_binary}；"
            "请先修复 SUMO 安装或设置 SUMO_BINARY；未启动任何服务"
        )
    match = re.search(
        r"\b(?:Version|sumo)\s+v?(\d+)[._](\d+)[._](\d+)(?=\s|\+|-|$)",
        result.stdout,
        re.IGNORECASE,
    )
    if not match:
        raise StartupError(f"无法识别 SUMO 版本：{sumo_binary}；请检查该程序的 --version 输出")
    version = ".".join(str(int(part)) for part in match.groups())
    if version not in SUPPORTED_SUMO_VERSIONS:
        raise StartupError(
            f"当前 SUMO 为 {version}，统一入口支持 {' / '.join(SUPPORTED_SUMO_VERSIONS)}；"
            "请检查 SUMO_BINARY 或 SUMO_HOME 指向的程序"
        )
    return version


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
    reuse_check: Callable[[], None] | None = None
    optional: bool = False


@dataclass(frozen=True)
class ServiceProcess:
    pid: int
    parent_pid: int
    uid: int | str
    started: str
    command: tuple[str, ...]
    cwd: Path


class WindowsProcesses:
    """Read native process identities and stop only a verified PID lifetime."""

    def __init__(self):
        try:
            self.api = importlib.import_module("psutil")
        except ImportError as exc:
            raise StartupError("Windows 重启需要 psutil，请使用当前 Python 安装 requirements.txt") from exc
        try:
            self.owner = self.api.Process().username().casefold()
        except self.api.Error as exc:
            raise StartupError("无法确认当前 Windows 用户，未停止任何服务") from exc

    def listening_pids(self, port):
        try:
            connections = self.api.net_connections(kind="tcp")
            listeners = [item for item in connections if item.status == self.api.CONN_LISTEN
                         and item.laddr.port == port]
            if any(not item.pid for item in listeners):
                raise StartupError(f"无法确定 {port} 的监听进程，未停止任何服务")
            return {item.pid for item in listeners}
        except self.api.Error as exc:
            raise StartupError("无法读取 Windows 监听进程，未停止任何服务") from exc

    def identity(self, pid):
        try:
            process = self.api.Process(pid)
            with process.oneshot():
                command, cwd = tuple(process.cmdline()), process.cwd()
                if not command or not cwd:
                    raise StartupError(f"无法核对进程 {pid} 的身份，未停止它")
                return ServiceProcess(pid, process.ppid(), process.username().casefold(),
                                      repr(process.create_time()), command, Path(cwd).resolve())
        except self.api.NoSuchProcess:
            return None
        except (self.api.Error, OSError) as exc:
            raise StartupError(f"无法核对 Windows 进程 {pid} 的身份，未停止它") from exc

    def lifetime(self, pid):
        try:
            process = self.api.Process(pid)
            return process.username().casefold(), repr(process.create_time())
        except self.api.NoSuchProcess:
            return None
        except self.api.Error as exc:
            raise StartupError(f"无法核对 Windows 进程 {pid} 的退出状态") from exc

    def has_children(self, pids):
        try:
            for pid in pids:
                try:
                    if self.api.Process(pid).children():
                        return True
                except self.api.NoSuchProcess:
                    continue
            return False
        except self.api.Error as exc:
            raise StartupError("无法核对仿真子进程，未停止任何服务") from exc

    def terminate(self, identity):
        try:
            process = self.api.Process(identity.pid)
            if (process.username().casefold(), repr(process.create_time())) != (identity.uid, identity.started):
                raise StartupError("服务进程已变化，未停止该进程")
            # psutil also checks for PID reuse immediately before termination.
            process.terminate()
        except self.api.NoSuchProcess:
            return
        except self.api.Error as exc:
            raise StartupError(f"无法停止 Windows 进程 {identity.pid}，请手动检查") from exc


def process_query(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=5,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode not in (0, 1):
        raise StartupError("无法核对本机进程，未强制停止服务；请先手动停止旧服务")
    return result.stdout.strip()


def process_lifetime(pid):
    """Identify a live PID without requiring files it releases during exit."""
    line = process_query(["ps", "-p", str(pid), "-o", "uid=,stat=,lstart="])
    if not line:
        return None
    fields = line.split()
    if len(fields) >= 2 and fields[1].startswith("Z"):
        return None
    if len(fields) != 7 or not fields[0].isdigit():
        raise StartupError(f"无法核对进程 {pid} 的退出状态，请检查后重试")
    return int(fields[0]), " ".join(fields[2:])


def process_identity(pid):
    line = process_query(["ps", "-p", str(pid), "-o", "uid=,ppid=,stat=,lstart=,args="])
    if not line:
        return None
    fields = line.split(maxsplit=8)
    if len(fields) == 9 and fields[2].startswith("Z"):
        return None
    cwd = process_query(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    paths = [line[1:] for line in cwd.splitlines() if line.startswith("n")]
    if len(fields) != 9 or len(paths) != 1:
        # The process may have exited between ps and lsof. Live processes
        # with unreadable identities still fail the ownership check.
        if process_lifetime(pid) is None:
            return None
        raise StartupError(f"无法核对进程 {pid} 的身份，未停止它")
    return ServiceProcess(pid, int(fields[1]), int(fields[0]), " ".join(fields[3:8]),
                          tuple(shlex.split(fields[8])), Path(paths[0]).resolve())


def matches_service(process, service, *, owner=None):
    if process.cwd != ROOT.resolve() or process.uid != (os.geteuid() if owner is None else owner):
        return False
    command = list(process.command)
    executable_pattern = r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?" if WINDOWS else r"python(?:\d+(?:\.\d+)*)?"
    if not command or not re.fullmatch(executable_pattern, Path(command.pop(0)).name, re.IGNORECASE if WINDOWS else 0):
        return False
    while command and command[0] in {"-u", "-B"}:
        command.pop(0)
    expected = service.command[1:]
    if expected[:1] == ["-m"]:
        return command[:2] == expected[:2]
    return bool(command) and (process.cwd / command[0]).resolve() == (ROOT / expected[0]).resolve()


def restart_local_services(services):
    """Explicit local-only restart. Validate every target before stopping any."""
    windows = WindowsProcesses() if WINDOWS else None
    if not windows and not all(shutil.which(tool) for tool in ("ps", "lsof")):
        raise StartupError("--restart-services 需要 macOS/Linux 的 ps 和 lsof；普通启动不受影响")
    identity_of = windows.identity if windows else process_identity
    lifetime_of = windows.lifetime if windows else process_lifetime
    owner = windows.owner if windows else os.geteuid()
    targets = []
    for service in services:
        if service.port not in {8765, 8766, 8767, 8768, 8769}:
            raise StartupError("重启模式仅用于本机 8765～8769，未停止任何服务")
        if windows:
            pids = windows.listening_pids(service.port)
        else:
            output = process_query(["lsof", "-nP", "-a", f"-iTCP:{service.port}", "-sTCP:LISTEN", "-Fp"])
            pids = {int(line[1:]) for line in output.splitlines() if line.startswith("p") and line[1:].isdigit()}
        if not pids:
            if listening(service.port):
                raise StartupError(f"无法确定 {service.port} 的监听进程，未停止任何服务")
            continue
        if len(pids) != 1:
            raise StartupError(f"{service.port} 存在多个监听进程，未停止任何服务")
        process = identity_of(pids.pop())
        if process is None:
            continue
        if not matches_service(process, service, owner=owner):
            raise StartupError(f"{service.port} 不属于本用户的当前项目服务，未停止任何服务")
        if process.parent_pid > 1:
            parent = identity_of(process.parent_pid)
            if parent and parent.cwd == ROOT.resolve() and any(
                (Path(arg).name.casefold() if WINDOWS else Path(arg).name) == "start_backend.py" for arg in parent.command[1:]
            ):
                raise StartupError("旧后端启动器仍在运行（run.sh / run.ps1），请先在其终端按 Ctrl+C；未停止任何服务")
        targets.append((service, process))

    if any(service.port == 8766 for service, _ in targets):
        try:
            health = get_json(8766, "/crowdSim/control/health")["data"]
            idle = ("activeRunId" in health and health["activeRunId"] is None
                    and type(health.get("queuedRuns")) is int and health["queuedRuns"] == 0)
        except Exception as exc:
            raise StartupError("无法确认实验状态，请先手动停止旧服务；未停止任何服务") from exc
        if not idle:
            raise StartupError("存在运行中或排队的实验，请先结束实验；未停止任何服务")
    simulator_pids = {process.pid for service, process in targets if service.port == 8765}
    if simulator_pids:
        # SUMO stays alive for paused runs too. Do not discard these sessions.
        if windows:
            has_children = windows.has_children(simulator_pids)
        else:
            rows = process_query(["ps", "-axo", "pid=,ppid="])
            has_children = any(len(parts := row.split()) == 2 and int(parts[1]) in simulator_pids for row in rows.splitlines())
        if has_children:
            raise StartupError("仿真服务仍有子进程，请先结束运行中或暂停的仿真；未停止任何服务")

    # Check PID/start time again to avoid signalling a recycled or replaced PID.
    for _, process in targets:
        current = identity_of(process.pid)
        if current is not None and current != process:
            raise StartupError("服务进程已变化，请重试；未停止任何服务")
    order = {8766: 0, 8765: 1, 8767: 2, 8768: 3, 8769: 4}
    for service, process in sorted(targets, key=lambda item: order[item[0].port]):
        current = identity_of(process.pid)
        if current is None:
            continue
        if current != process:
            raise StartupError(f"{service.port} 进程已变化，未停止该进程")
        report(f"重启本机 {service.name}（{service.port}），停止旧进程 {process.pid}")
        try:
            if windows:
                windows.terminate(process)
            else:
                os.kill(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        deadline = time.monotonic() + 5
        # After termination, cwd/arguments can disappear before the OS stops listing
        # the process. Only its PID lifetime is needed to wait for exit; never
        # retry a signal against a PID that has since been reused.
        while lifetime_of(process.pid) == (process.uid, process.started):
            if time.monotonic() >= deadline:
                raise StartupError(f"{service.port} 旧进程未退出，请手动检查；不会强制结束未知进程")
            time.sleep(0.1)
    if targets:
        report("本机旧服务已停止；随后启动的新服务将在本终端退出时清理")


class Supervisor:
    def __init__(self, *, reuse_existing=True):
        self.children = []
        self.reuse_existing = reuse_existing
        self.optional_names = set()
        self.unavailable_services = set()

    def spawn(self, name, command, *, interactive=False, critical=True):
        report(f"启动 {name}")
        # SSH reads passwords from /dev/tty, so it must keep the launcher's
        # controlling terminal. Background services still own isolated groups.
        owns_process_group = os.name != "nt" and not interactive
        child = subprocess.Popen(command, cwd=ROOT, env={**os.environ, "PYTHONUNBUFFERED": "1"},
                                 stdin=None if interactive else subprocess.DEVNULL,
                                 start_new_session=owns_process_group)
        self.children.append((name, child, owns_process_group))
        if not critical:
            self.optional_names.add(name)
        return child

    def check_children(self):
        for name, child, _ in list(self.children):
            if child.poll() is not None:
                if name in self.optional_names:
                    self.unavailable_services.add(name)
                    report(f"{name} 已退出（退出码 {child.returncode}）；原有服务继续运行")
                    self._discard_children([item for item in self.children if item[0] == name])
                    continue
                raise StartupError(f"{name} 已退出（退出码 {child.returncode}），请检查上方日志")

    def wait_ready(self, name, probe, timeout=60, *, child=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_children()
            if child is not None and child.poll() is not None:
                raise StartupError(f"{name} 已退出（退出码 {child.returncode}）")
            try:
                if probe():
                    report(f"{name} 已就绪")
                    return
            except Exception:
                pass
            time.sleep(0.3)
        raise StartupError(f"{name} 在 {timeout} 秒内未就绪，请检查端口、认证和远程服务")

    def start_service(self, service):
        checkpoint = len(self.children)
        try:
            self._start_service(service)
        except (StartupError, OSError) as exc:
            if not service.optional:
                raise
            self._discard_children(self.children[checkpoint:])
            self.unavailable_services.add(service.name)
            report(f"{service.name} 未就绪：{exc}；原有服务继续运行")
            # A failure of a required child must still stop the launcher.
            self.check_children()
            return False
        if service.optional:
            self.optional_names.add(service.name)
        return True

    def _start_service(self, service):
        if listening(service.port):
            if not self.reuse_existing:
                raise StartupError(f"重启期间 {service.port} 被再次占用，未复用或停止新占用者，请检查后重试")
            try:
                valid = service.probe()
            except Exception:
                valid = False
            if not valid:
                raise StartupError(f"{service.port} 已被占用，但不是可用的 {service.name}；请检查该端口")
            if service.reuse_check is not None:
                service.reuse_check()
            report(f"复用已有 {service.name}（{service.port}），退出时不会关闭它")
            return
        self.spawn(service.name, service.command)
        self.wait_ready(service.name, service.probe)

    def _discard_children(self, children):
        """Clean up only children created for the failed optional service."""
        cleanup = Supervisor()
        cleanup.children = list(children)
        removed = {child.pid for _, child, _ in children}
        self.children = [item for item in self.children if item[1].pid not in removed]
        cleanup.close()

    def close(self):
        # Only stop processes created by this launcher, including their descendants.
        for name, child, owns_process_group in reversed(self.children):
            report(f"停止 {name}")
            try:
                if not owns_process_group:
                    if child.poll() is None:
                        child.terminate()
                else:
                    os.killpg(child.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        deadline = time.monotonic() + 5
        for _, child, owns_process_group in reversed(self.children):
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                if not owns_process_group:
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


def port_bound(port):
    # Inspect local state without opening an SSH channel to the model server.
    if WINDOWS:
        # SO_REUSEADDR on Windows can bind over a live SSH listener, so a
        # successful bind does not establish that the port is free.
        api = importlib.import_module("psutil")
        try:
            return any(connection.status == api.CONN_LISTEN
                       and connection.laddr.port == port
                       and connection.laddr.ip in {"127.0.0.1", "0.0.0.0"}
                       for connection in api.net_connections(kind="tcp4"))
        except api.Error as exc:
            raise StartupError("无法读取 Windows 本机端口监听状态，请检查系统权限") from exc
    with socket.socket() as candidate:
        candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            candidate.bind(("127.0.0.1", port))
            return False
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return True
            raise


def ensure_tunnels(supervisor, *, enabled=True):
    from data_service.repositories.datasets import DatasetRepository
    from data_service.connectors.models import model_configs, MODEL_SPECS
    from urllib.parse import urlsplit
    repository = DatasetRepository()
    pending = []
    try:
        database_probe = lambda: repository.status()["connected"]
        try:
            database_ready = database_probe()
        except Exception:
            database_ready = False
        if database_ready:
            report("数据库连接已可用，复用现有连接")
        else:
            if not enabled:
                raise StartupError("数据库连接不可用，且已禁用自动 SSH 隧道")
            if port_bound(27018):
                raise StartupError("27018 已占用但数据库不可用，请检查隧道或认证配置")
            if os.environ.get("CROWDSIM_MONGO_URI") or os.environ.get("CROWDSIM_MONGO_HOST", "127.0.0.1") != "127.0.0.1" or int(os.environ.get("CROWDSIM_MONGO_PORT", "27018")) != 27018:
                raise StartupError("自定义 MongoDB 连接不可用，请检查配置")
            settings, remote_host, remote_port = tunnel_settings("DB")
            pending.append(("DB", 27018, database_probe, settings, remote_host, remote_port))

        providers = model_configs()
        scheduled_ports = {}
        for identifier, label, prefix, default_port, _ in MODEL_SPECS:
            provider = providers[identifier]
            target = urlsplit(provider["base_url"])
            if target.hostname not in ("127.0.0.1", "localhost"):
                continue
            port = target.port or provider["port"]
            if port_bound(port):
                report(f"复用 {label} 本机端口（{port}），模型可达性在展开菜单时检查")
                continue
            if not enabled:
                report(f"{label} 的 {port} 端口映射未建立，其他服务继续启动")
                continue
            try:
                settings, common_host, common_port = tunnel_settings("MODEL")
            except StartupError:
                report(f"{label} 未配置模型 SSH，其他服务继续启动")
                continue
            remote_host = os.environ.get(f"CROWDSIM_{prefix}_REMOTE_HOST", common_host)
            remote_port = os.environ.get(f"CROWDSIM_{prefix}_REMOTE_PORT", common_port if prefix in ("QWEN", "GLM_MAIN") else str(default_port))
            if not 1 <= port <= 65535 or not 1 <= int(remote_port) <= 65535 or remote_host.startswith("-") or any(c.isspace() for c in remote_host):
                raise StartupError(f"{label} 端口映射配置无效")
            destination = (settings['HOST'], settings['PORT'], settings['USER'], remote_host, remote_port)
            if port in scheduled_ports:
                if scheduled_ports[port] != destination:
                    raise StartupError(f'本机 {port} 配置了不同的模型映射目标，请检查 .env')
                continue  # Multiple model names may share one OpenAI-compatible service.
            scheduled_ports[port] = destination
            pending.append((label, port, lambda port=port: port_bound(port), settings, remote_host, remote_port))

        groups = {}
        for item in pending:
            settings = item[3]
            groups.setdefault((settings["HOST"], settings["PORT"], settings["USER"]), []).append(item)
        for (host, port, user), items in groups.items():
            critical = any(item[0] == "DB" for item in items)
            command = ["ssh", "-F", os.devnull, "-N", "-o", "ExitOnForwardFailure=yes",
                       "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=10", "-p", port]
            identity = os.environ.get("CROWDSIM_SSH_IDENTITY_FILE")
            if identity:
                command.extend(["-i", str(Path(identity).expanduser())])
            for _, local_port, _, _, remote_host, remote_port in items:
                command.extend(["-L", f"127.0.0.1:{local_port}:{remote_host}:{remote_port}"])
            command.append(f"{user}@{host}")
            child = supervisor.spawn("SSH " + "+".join(item[0] for item in items), command, interactive=True, critical=critical)
            for kind, _, probe, *_ in items:
                try:
                    supervisor.wait_ready(f"{kind} 隧道", probe, timeout=60 if kind == "DB" else 15, child=child)
                except StartupError as exc:
                    if critical:
                        raise
                    report(f"{exc}；其他服务继续启动，可稍后恢复模型映射")
                    break
    finally:
        repository.close()


def check_performance_reuse(options):
    from crowdsim.infrastructure.performance_probe import performance_options
    requested = performance_options(options)
    expected = {'enabled': requested.enabled, 'system_enabled': requested.enabled and requested.system_enabled,
                'interval_wall_seconds': requested.interval}
    try:
        actual = get_json(8765, '/api/v1/performance/config')
    except HTTPError as exc:
        # Legacy servers have no monitoring endpoint. They remain reusable
        # for ordinary startup; explicit monitoring still needs a new server.
        if exc.code == 404 and not requested.enabled:
            return
        raise StartupError('已有 SUMO 服务无法确认监测设置；请先关闭并按本次参数重启 SUMO 服务') from exc
    except Exception as exc:
        raise StartupError('已有 SUMO 服务无法确认监测设置；请先关闭并按本次参数重启 SUMO 服务') from exc
    if not isinstance(actual, dict) or any(actual.get(key) != value for key, value in expected.items()):
        raise StartupError('已有 SUMO 服务的监测参数与本次不同；请先关闭并按本次参数重启 SUMO 服务')


def main(argv=None):
    parser = argparse.ArgumentParser(description="一键启动 CrowdSim 仿真、实验控制、对话、数据服务及 SSH 隧道",
        epilog="仿真参数会转发给 SUMO 服务；添加 --monitor-performance 开启诊断，"
               "--performance-interval 5 设置采样间隔，--performance-no-system 关闭系统采样。默认不记录开销数据。")
    parser.add_argument("--scenario", choices=("research", "hotspot", "east-nanjing-road"), default="hotspot")
    parser.add_argument("--mode", choices=("rule", "llm"), default="rule")
    parser.add_argument("--sumo-binary")
    parser.add_argument("--no-tunnels", action="store_true", help="复用已有连接，不创建 SSH 隧道")
    parser.add_argument("--check", action="store_true", help="只检查依赖、SUMO 和配置，不启动进程")
    parser.add_argument("--restart-services", action="store_true", help="显式重启本用户、当前项目的本机 8765～8769 服务；不重启远程服务或现有隧道")
    args, simulation_args = parser.parse_known_args(argv)
    supervisor = Supervisor(reuse_existing=not args.restart_services)
    try:
        def request_shutdown(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, request_shutdown)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, request_shutdown)
        if sys.version_info < (3, 10):
            raise StartupError("统一启动需要 Python 3.10 或更新版本，请使用安装了 requirements.txt 的解释器")
        report(f"检查后端环境：Python {sys.version.split()[0]}（{sys.executable}）")
        from crowdsim.windows_platform import configure_windows_platform
        configure_windows_platform()
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
        os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
        report("正在检查 Python 服务依赖…")
        check_service_dependencies()
        report("服务依赖检查通过，正在检查 SUMO 与场景配置…")
        from crowdsim.infrastructure.sumo_adapter import discover_sumo_binary
        from crowdsim_overlay_server import parse_args, resolve_service_scenario
        sumo_args = ["--scenario", args.scenario, "--mode", args.mode, *simulation_args]
        if args.sumo_binary:
            sumo_args += ["--sumo-binary", args.sumo_binary]
        options = parse_args(sumo_args)
        if options.host not in ("127.0.0.1", "localhost") or options.port != 8765:
            raise StartupError("统一入口固定使用本机 8765；自定义仿真监听地址请使用原仿真入口")
        sumo_binary = discover_sumo_binary(args.sumo_binary)
        sumo_version = check_sumo_version(sumo_binary)
        report(f"使用 SUMO {sumo_version}：{sumo_binary}")
        resolve_service_scenario(options)
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
                    lambda: get_json(8765, "/api/v1/post/capabilities").get("data", {}).get("contractVersion") == "post-api/1.0",
                    lambda: check_performance_reuse(options)),
            Service("新闻采集服务", 8769, [sys.executable, "-m", "data_service.crawlers"],
                    lambda: get_json(8769, "/api/crowdSim/news-collection/status").get("data", {}).get("service") == "crowdsim-news-crawler",
                    optional=True),
            Service("实验控制服务", 8766,
                    [sys.executable, "-m", "crowdsim.experiments", "--host", "127.0.0.1", "--port", "8766",
                     "--gateway-url", "ws://127.0.0.1:8765"],
                    lambda: get_json(8766, "/crowdSim/control/health").get("data", {}).get("orchestrator") == "ready",
                    optional=True),
        ]
        if args.restart_services:
            restart_local_services(services)
        control_ready = True
        crawler_ready = True
        for service in services:
            ready = supervisor.start_service(service)
            if service.port == 8769:
                crawler_ready = ready is not False
            if service.port == 8766:
                control_ready = ready is not False
        report("采集 8769：" + ("不可用，其他服务继续运行" if not crawler_ready else "就绪") + "；可选模型映射 8899 / 8500")
        if control_ready:
            report("全部就绪：仿真 8765 / 实验控制 8766 / 对话 8767 / 数据 8768 / 模型 8800 / MongoDB 27018；Ctrl+C 退出")
        else:
            report("原有服务就绪：仿真 8765 / 对话 8767 / 数据 8768 / 模型 8800 / MongoDB 27018；实验控制 8766 不可用；Ctrl+C 退出")
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
