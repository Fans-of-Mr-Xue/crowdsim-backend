"""Private forecast child: normal class-two runtime, temporary archive, no server."""
import json
from pathlib import Path
import sys


def run(folder):
    from crowdsim_overlay_server import build_requirement_runtime, parse_args
    from .class2_forecast import BaselineCollector

    folder = Path(folder)
    request = json.loads((folder/'request.json').read_text())
    record = request['requirement']
    if (record['requirement'].get('research', {}).get('problem_type') != 2
            or record['requirement'].get('class2_demo', {}).get('seed') != request['seed']):
        raise ValueError('预演只接受已冻结的第二类人流配置与种子')
    runtime = build_requirement_runtime(parse_args(['--scenario', 'hotspot', '--sumo-binary', request['sumoBinary']]), record)
    runtime.archive_root = folder/'runs'
    try:
        runtime.initialize()
        references = runtime.class2_flows.control.context(runtime)['predictedOverlaps']
        collector = BaselineCollector(runtime, [row['edgeId'] for row in references])
        collector.sample(runtime)
        runtime.start()
        while runtime.running:
            runtime.tick()
            if len(runtime.current.persons) != len(runtime.class2_flows.people):
                raise ValueError('预演人数不守恒')
            collector.sample(runtime)
        if runtime.time_seconds != runtime.timeline_end_seconds:
            raise ValueError('基线预演未到达方案结束时刻')
        result = collector.result(runtime)
        result['seed'] = runtime.random_seed
        result['networkSha256'] = runtime.network_sha256
        result['engineVersion'] = runtime.adapter.diagnostics['sumo_version']
        runtime.close()
        if runtime.close_errors:
            raise ValueError('基线预演引擎未正常关闭')
        (folder/'result.json').write_text(json.dumps(result, ensure_ascii=False))
    finally:
        runtime.close()


if __name__ == '__main__':
    run(sys.argv[1])
