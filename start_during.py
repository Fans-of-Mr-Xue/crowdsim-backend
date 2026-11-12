"""Opt-in during-only launcher: local services, no SSH, MongoDB or post startup."""
import argparse
import os
from pathlib import Path
import signal
import sys
import time

from start_backend import (ROOT, Service, Supervisor, StartupError, check_service_dependencies,
                           check_sumo_version, get_json, report, restart_local_services)


def during_services(simulation_args):
    return [
        Service("事中模型网关", 8768, [sys.executable, "-m", "crowdsim.during_models"],
                lambda: get_json(8768, "/api/crowdSim/during/health").get("service") == "crowdsim-during-models"),
        Service("对话存储", 8767, [sys.executable, "-m", "dialogue_service.server"],
                lambda: get_json(8767, "/health").get("service") == "crowdsim-dialogue-storage"),
        Service("事中仿真", 8765, [sys.executable, "-m", "crowdsim.during_server", *simulation_args],
                lambda: get_json(8765, "/api/v1/during/health").get("service") == "crowdsim-during-simulator"),
        Service("实验控制服务", 8766, [sys.executable, "-m", "crowdsim.experiments", "--host", "127.0.0.1", "--port", "8766", "--gateway-url", "ws://127.0.0.1:8765"],
                lambda: get_json(8766, "/crowdSim/control/health").get("data", {}).get("orchestrator") == "ready"),
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description="事中独立启动：不连接 MongoDB、不建立 SSH 隧道、不启动事后接口")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--restart-services", action="store_true")
    parser.add_argument("--scenario", choices=("research", "hotspot", "east-nanjing-road"), default="hotspot")
    args, extra = parser.parse_known_args(argv)
    supervisor = Supervisor(reuse_existing=not args.restart_services)
    try:
        def shutdown(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, shutdown)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, shutdown)
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
        from crowdsim.windows_platform import configure_windows_platform
        configure_windows_platform()
        os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
        check_service_dependencies()
        from crowdsim_overlay_server import parse_args, resolve_service_scenario
        from crowdsim.infrastructure.sumo_adapter import discover_sumo_binary
        simulation_args = ["--scenario", args.scenario, "--mode", "rule", *extra]
        options = parse_args(simulation_args)
        if options.host not in {"127.0.0.1", "localhost"} or options.port != 8765 or options.mode != "rule":
            raise StartupError("事中独立入口固定使用本机8765及rule启动；个体LLM由每轮事中模型选择绑定")
        check_sumo_version(discover_sumo_binary(options.sumo_binary))
        resolve_service_scenario(options)
        report("事中检查通过；模型：GLM-5.3-Flash（本机8800）")
        if args.check:
            report("检查结束；未启动服务、未连接数据库或远程模型")
            return 0
        services = during_services(simulation_args)
        if args.restart_services:
            restart_local_services(services, launcher_names={"start_backend.py", "start_during.py"})
        for service in services:
            supervisor.start_service(service)
        report("事中就绪：仿真8765 / 实验8766 / 对话8767 / 模型8768；请保持GLM的8800连接可用。Ctrl+C退出")
        while True:
            supervisor.check_children()
            time.sleep(1)
    except KeyboardInterrupt:
        report("收到退出请求")
        return 0
    except (StartupError, ImportError, OSError, ValueError) as exc:
        report(f"事中启动失败：{exc}")
        return 1
    finally:
        supervisor.close()


if __name__ == "__main__":
    raise SystemExit(main())
