#!/usr/bin/env python3
"""Export saved observations; no server, browser dependencies or SUMO needed."""

import argparse
from pathlib import Path
import sys
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crowdsim.infrastructure.experiment_viewer import build_viewer


def main():
    parser = argparse.ArgumentParser(description="从后端实验记录生成离线指标查看器")
    parser.add_argument("--runs-root", type=Path, default=ROOT / "runs")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/experiment_viewer")
    parser.add_argument("--run", action="append", dest="run_ids", help="指定 run_id，可重复；默认读取全部观测实验")
    parser.add_argument("--max-map-frames", type=int, default=1200, help="每轮空间图帧数上限，全局曲线不降采样（默认 1200）")
    parser.add_argument("--png", action="store_true", help="额外导出十项指标的静态 PNG，需安装 matplotlib")
    parser.add_argument("--open", action="store_true", dest="open_browser", help="生成成功后自动用默认浏览器打开查看器")
    parser.add_argument("--scope-reference-file", type=Path, help="范围对照 JSON；默认使用 config/viewer_scope_references.json，仅展示差异，不重算旧指标")
    args = parser.parse_args()
    try:
        data = build_viewer(args.runs_root, args.output, args.run_ids, args.max_map_frames, args.png, args.scope_reference_file)
    except (OSError, ValueError, ImportError) as error:
        parser.exit(1, f"导出失败：{error}\n")
    viewer = (args.output / "index.html").resolve()
    print(f"查看器：{viewer}")
    for run in data["runs"]:
        scope_info = run["scope_info"]
        print(f"  {run['scene_name']} | {run['id']} | {len(run['samples'])} 帧 | {run['status']}"
              f" | 范围 {scope_info['version']} | {scope_info['status']}")
    for skipped in data["skipped"]:
        print(f"  已跳过 {skipped['run_id']}: {skipped['reason']}", file=sys.stderr)
    if args.open_browser:
        try:
            opened = webbrowser.open(viewer.as_uri(), new=2)
        except webbrowser.Error:
            opened = False
        if not opened:
            parser.exit(1, f"无法自动打开浏览器，请手动打开：{viewer}\n")
        print(f"[CrowdSim] 已请求浏览器打开：{viewer}")


if __name__ == "__main__":
    main()
