"""Use the execution path itself to validate a dataset before showing a pass."""
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess

from postanalysis_api.schemas.common import ApiError
from postanalysis_api.workers.memorial_scenario import NET_FILE, compile_routes
from .execution_template import execution_readiness, execution_template


def validate_execution_input(app, dataset, plan=None):
    from .population_execution import prepare_execution_dataset, execution_context
    readiness = execution_readiness(dataset, plan)
    result = {**readiness, "datasetId": dataset["id"], "datasetVersion": dataset["version"],
              "checks": []}
    if not readiness["ready"]:
        return result
    try:
        dataset = prepare_execution_dataset(dataset)
        task = execution_template(dataset, plan)
        # The same checks guard /experiments/{id}/start.
        app._check_start_capability(task)
        with TemporaryDirectory(prefix="post-input-check-") as folder:
            output = Path(folder) / "demand.rou.xml"
            compile_routes(dataset, task["seedStart"], [], task["scenario"]["durationSeconds"], output)
            for index, group in enumerate(task["groups"]):
                compile_routes(dataset, task["seedStart"], group["interventions"],
                               task["scenario"]["durationSeconds"], Path(folder) / f"group-{index}.rou.xml")
            binary = getattr(app.executor, "binary", None)
            if not binary:
                raise ApiError("SUMO_BINARY_MISSING", "请配置可用的SUMO运行程序后重新校验。", 503)
            from .factual_context import is_spatial
            from postanalysis_api.workers.distribution_demand import sumo_parameters
            parameters = sumo_parameters(dataset['metadata']['factualContext']) if is_spatial(dataset) else ['--step-length', '0.5']
            probe = subprocess.run([binary, "--net-file", str(NET_FILE), "--route-files", str(output), *parameters,
                                    "--begin", "0", "--end", "0.5",
                                    "--no-step-log", "true", "--no-warnings", "true"],
                                   capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if probe.returncode:
                raise ApiError("SUMO_INPUT_REJECTED", "SUMO未能载入当前路网和人员输入，请检查运行程序与路网。", 422)
        result.update(message="校验通过，可以进行仿真。", executionContext=execution_context(dataset), checks=["源流量与人员初始化", "场景与路网版本", "行人路径编译", "控制动作与指标", "SUMO运行器"])
    except ApiError as exc:
        result.update(ready=False, code=exc.code, message=exc.message, missing=[exc.message],
                      nextAction="修正后重新校验。")
    except (OSError, ValueError, ImportError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result.update(ready=False, code="EXECUTION_PREFLIGHT_FAILED", message=f"运行输入检查失败：{exc}",
                      missing=["请检查SUMO安装、路网及行人路径"], nextAction="修正后重新校验。")
    return result
