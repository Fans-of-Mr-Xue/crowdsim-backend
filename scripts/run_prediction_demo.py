"""Run the actual three-model worker without Mongo/OSM/LLM, then export a viewer."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data_service.prediction.demo import demo_inputs, monument_inputs
from data_service.prediction.solver import compute_comparison


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs')
    parser.add_argument('--scene', choices=['monument', 'synthetic'], default='monument')
    args = parser.parse_args()
    result = compute_comparison(*(monument_inputs() if args.scene == 'monument' else demo_inputs()), ['conservation', 'collision_free_speed', 'social_force'])
    args.output.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
    (args.output / 'three-model-demo.json').write_text(serialized, encoding='utf-8')
    template = (ROOT / 'scripts' / 'prediction_demo.html').read_text(encoding='utf-8')
    (args.output / 'three-model-demo.html').write_text(template.replace('__RESULT_JSON__', serialized.replace('<', '\\u003c')), encoding='utf-8')
    for item in result['results']:
        tail = item['series'][-1]
        print(f"{item['method']['name']}: 剩余 {tail['crowd']}，离场 {tail['departed']}，守恒误差 {item['summary']['mass_balance_error']}")
    for item in result['method_errors']:
        print(f"未完成：{item['method']['name']}：{item['message']}", file=sys.stderr)
    print(args.output / 'three-model-demo.html')
    return 1 if result['method_errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
