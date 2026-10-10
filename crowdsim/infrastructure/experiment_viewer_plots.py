"""Optional static scientific plots using exactly the viewer's exported data."""

from pathlib import Path
import os


def export_pngs(data, output):
    cache = Path(output).parent / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache / "matplotlib"))
    os.environ.setdefault("XDG_CACHE_HOME", str(cache))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.colors import Normalize, TwoSlopeNorm
    from matplotlib.patches import PathPatch
    from matplotlib.lines import Line2D
    from matplotlib.path import Path as PlotPath
    import numpy as np

    available = {font.name for font in font_manager.fontManager.ttflist}
    fonts = [name for name in ["Arial Unicode MS", "PingFang SC", "Noto Sans CJK SC", "Microsoft YaHei", "SimHei", "DejaVu Sans"] if name in available]
    plt.rcParams.update({"font.family": fonts or ["sans-serif"], "axes.unicode_minus": False,
                         "font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    colors = ["#3975d7", "#14a08c", "#edac4f", "#9673c3", "#9caaba"]

    def polygons(geometry):
        if not geometry:
            return []
        if geometry["type"] == "Polygon":
            return [geometry["coordinates"]]
        if geometry["type"] == "MultiPolygon":
            return geometry["coordinates"]
        if geometry["type"] == "GeometryCollection":
            return [p for g in geometry["geometries"] for p in polygons(g)]
        return []

    def lines(geometry):
        if geometry["type"] == "LineString":
            return [geometry["coordinates"]]
        if geometry["type"] == "MultiLineString":
            return geometry["coordinates"]
        if geometry["type"] == "GeometryCollection":
            return [p for g in geometry["geometries"] for p in lines(g)]
        return []

    def curve(ax, run, fields, unit):
        times = [s["time_seconds"] for s in run["samples"]]
        for i, (field, label) in enumerate(fields):
            values = [s.get(field) for s in run["samples"]]
            ax.plot(times, [v if v is not None else np.nan for v in values], label=label,
                    color=colors[i % len(colors)], linewidth=1.6,
                    marker="o" if len(times) == 1 else None)
        ax.set(xlabel="仿真时间 / s", ylabel=unit)
        ax.grid(alpha=.18)
        ax.legend(loc="best", fontsize=9)

    def scope_outlines(ax, run, *, comparison=False):
        scope = run["geometry"]["scope"]
        info = run.get("scope_info") or {}
        reference = info.get("overlay_geometry") if info.get("status") == "changed" else None
        for geometry, color, style in [(scope, "#56708d", "-"), (reference, "#dc8c27", "--")]:
            for polygon in polygons(geometry):
                for ring in polygon:
                    ax.plot(*zip(*ring), color=color, linestyle=style, linewidth=1.2)
        points = [point for geometry in [scope, reference] for polygon in polygons(geometry) for ring in polygon for point in ring]
        if points:
            xs, ys = zip(*points)
            margin = max(max(xs)-min(xs), max(ys)-min(ys))*.05
            ax.set_xlim(min(xs)-margin, max(xs)+margin); ax.set_ylim(min(ys)-margin, max(ys)+margin)
        if reference or comparison:
            handles = [Line2D([0], [0], color="#56708d", label="本轮实际统计边界")]
            if reference:
                handles.append(Line2D([0], [0], color="#dc8c27", linestyle="--", label="已同步预设边界（仅对照）"))
            ax.legend(handles=handles, loc="upper left", fontsize=8)
        ax.set_aspect("equal", adjustable="box")
        ax.set(xlabel="SUMO x / m", ylabel="SUMO y / m")
        ax.ticklabel_format(style="plain", useOffset=False)

    def footer(fig, run, status):
        info = run.get("scope_info") or {}
        area = f"{run['area']:,.2f}" if run['area'] is not None else "未记录"
        note = " | 范围与预设不同，指标保留原值" if info.get("status") == "changed" else ""
        fig.text(.01,.012,f"{run['id']} | {status} | {run['status']} | 空值不视为零\n"
                 f"本轮统计范围：{info.get('version') or '未记录'} | {area} m²{note}", fontsize=8, color="#758397")

    def spatial(ax, run, values, maximum, title, boundary=False):
        cmap = plt.get_cmap("RdBu_r" if boundary else "Blues")
        norm = TwoSlopeNorm(vmin=-max(maximum, 1e-8), vcenter=0, vmax=max(maximum, 1e-8)) if boundary else Normalize(0, max(maximum, 1e-8))
        for i, cell in enumerate(run["geometry"]["cells"]):
            value = values[i] if not boundary else None
            for rings in polygons(cell["geometry"]):
                points, codes = [], []
                for ring in rings:
                    points.extend(ring)
                    codes.extend([PlotPath.MOVETO] + [PlotPath.LINETO] * (len(ring)-2) + [PlotPath.CLOSEPOLY])
                if points:
                    ax.add_patch(PathPatch(PlotPath(points, codes), facecolor=cmap(norm(value)) if value is not None else "#e6ebf1",
                                           edgecolor="#b9c8d8", linewidth=.35))
        for line in run["geometry"]["roads"]["lines"]:
            ax.plot(*zip(*line), color="#7b90a9", linewidth=.4, alpha=.6)
        if boundary:
            for i, item in enumerate(run["geometry"]["boundaries"]):
                value = values[i]
                for line in lines(item["geometry"]):
                    ax.plot(*zip(*line), color=cmap(norm(value)) if value is not None else "#bac3cf", linewidth=1.7)
        scope_outlines(ax, run)
        ax.set_title(title)
        if not any(value is not None for value in values):
            ax.text(.5, .5, "本阶段无有效观测", transform=ax.transAxes, ha="center", va="center",
                    bbox={"facecolor":"white", "alpha":.9, "edgecolor":"none"})
        fig = ax.figure
        fig.colorbar(plt.cm.ScalarMappable(norm=norm,cmap=cmap), ax=ax, shrink=.7,
                     label="m/s" if title.startswith("局部速度") else "人/m²")

    output = Path(output)
    for run in data["runs"]:
        directory = output / run["id"]
        directory.mkdir(parents=True, exist_ok=True)
        times = [s["time_seconds"] for s in run["samples"]]
        k = len(run["maps"]["indices"]) - 1
        time = run["samples"][run["maps"]["indices"][k]]["time_seconds"]
        for metric in data["metrics"]:
            code, name = metric["code"], metric["name"]
            count = 3 if code == "B3" else 2 if code in {"A1","A3","C1","C2"} else 1
            fig, axes = plt.subplots(1, count, figsize=(6*count, 5.2), squeeze=False)
            ax = axes[0][0]
            status = run["metrics"][metric["id"]]["status"]
            if status == "not_selected":
                ax.axis("off"); ax.text(.5,.5,"本轮未选择此指标",ha="center",transform=ax.transAxes)
            elif code == "A1":
                curve(ax,run,[("density_person_per_m2","区域密度")],"人/m²")
                curve(axes[0][1],run,[("person_count","区域内"),("network_person_count","全路网"),("outside_scope_person_count","区域外")],"人")
            elif code == "A3":
                curve(ax,run,[("avg_speed_mps","含停留平均速度"),("moving_avg_speed_mps","运动人员平均速度")],"m/s")
                curve(axes[0][1],run,[("speed_sample_count","有效样本"),("moving_person_count","运动人员"),("invalid_speed_count","无效速度")],"人")
            elif code in {"A2","A4","A5"}:
                key = {"A2":"density","A4":"speed","A5":"boundary"}[code]
                spatial(ax,run,run["maps"][key][k],run["ranges"][key],f"{name} · {time:g} s",code=="A5")
            elif code == "B3":
                for target,key,label in zip(axes[0],["initialRuntime","runtimeFinal","initialFinal"],
                                            ["|初始 − 运行时|","|运行时 − 结束|","|初始 − 结束|"]):
                    spatial(target,run,run["maps"][key][k],run["ranges"]["difference"],f"{label} · {time:g} s")
            elif code in {"B1","B2"}:
                e = run["evacuation"]
                ax.axis("off")
                value = e["duration"] if code == "B1" else e["efficiency"]
                text = f"{value:.6g} {metric['unit']}" if value is not None else "无最终值（缺少策略或正常结束证据）"
                ax.text(.5,.72,text,ha="center",transform=ax.transAxes,fontsize=13)
                timestamp = lambda t: f"{t:g} s" if t is not None else "未记录 / 未达到"
                ax.text(.5,.43,"\n".join(["t0 首次运行："+timestamp(e["t0"]),"ta 策略应用："+timestamp(e["ta"]),
                                         "te 正常到达："+timestamp(e["te"]),"完成口径：本轮所有行人正常完成 SUMO 行程"]),
                        ha="center",transform=ax.transAxes,linespacing=2)
            else:
                psychology = code == "C2"
                keys, labels = (["calm","tense","panic","unknown"],["平静","紧张","恐慌","未知"]) if psychology else (
                    ["walking","waiting","blocked","avoiding","unknown"],["行走","停留","受阻","避让","未知"])
                prefix = "psychology" if psychology else "behavior"
                stack_colors = [colors[1], colors[2], "#db6375", colors[4]] if psychology else colors
                values = [[s.get(f"{prefix}_{key}_count") for s in run["samples"]] for key in keys]
                ax.stackplot(times, *[[v if v is not None else np.nan for v in row] for row in values], labels=labels, colors=stack_colors)
                if len(times)==1:
                    bottom=0
                    for row,label,color in zip(values,labels,stack_colors):
                        if row[0] is not None: ax.bar(times[0],row[0],bottom=bottom,color=color,width=.1);bottom+=row[0]
                ax.set(xlabel="仿真时间 / s",ylabel="区域内人数");ax.legend(fontsize=9);ax.grid(alpha=.18)
                if psychology:
                    curve(axes[0][1],run,[("stress_avg","压力"),("fatigue_avg","疲劳"),("perceived_risk_avg","感知风险")],"模型值 0–1")
                    axes[0][1].set_ylim(0,1.05)
                else:
                    transitions = [t.get("behavior_state",0) if t is not None else np.nan for t in run["transitions"]]
                    axes[0][1].plot(times,transitions,color=colors[3],marker="o" if len(times)==1 else None)
                    axes[0][1].set(xlabel="仿真时间 / s",ylabel="实际行为转换次数（排除首次标签）")
            fig.suptitle(f"{run['scene_name']} · {code} {name}",fontsize=14)
            footer(fig, run, status)
            fig.tight_layout(rect=(0,.08,1,.94))
            fig.savefig(directory / f"{code}.png", dpi=160)
            plt.close(fig)
        info = run.get("scope_info") or {}
        if info.get("status") == "changed" and info.get("overlay_geometry"):
            fig, ax = plt.subplots(figsize=(8, 6.5))
            for polygon in polygons(run["geometry"]["scope"]):
                ax.fill(*zip(*polygon[0]), facecolor="#e8eff8", alpha=.8)
            for line in run["geometry"]["roads"]["lines"]:
                ax.plot(*zip(*line), color="#7b90a9", linewidth=.5, alpha=.7)
            scope_outlines(ax, run, comparison=True)
            reference = info["reference"]
            ax.set_title(f"{run['scene_name']} · 范围边界对照\n"
                         f"本轮 {run['area']:,.2f} m² / {info['vertex_count']} 点；"
                         f"预设 {reference['area_m2']:,.2f} m² / {reference['vertex_count']} 点")
            footer(fig, run, "边界对照；无指标重算")
            fig.tight_layout(rect=(0,.08,1,1))
            fig.savefig(directory / "scope_comparison.png", dpi=160)
            plt.close(fig)
