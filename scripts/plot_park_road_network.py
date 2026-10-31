"""Render the complete Huangpu Park/monument road and junction numbering map.

Geometry comes from the current SUMO network; historical numbering and colors
come from the maintained numbering JSON. Output PNG, zoomable SVG and a JSON
snapshot containing the network hash. Requires matplotlib and sumolib.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon, Rectangle
from matplotlib.ticker import MaxNLocator
import sumolib

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "scenarios/shanghai_bund"
BACKGROUND = "#f6f8fb"
INK = "#253349"
MUTED = "#718099"
NEW_ROADS = {"M3", "P20a", "P20b", "P20c", "P21", "P22", "P23", "P24", "P25", "P26", "P27a", "P27b", "P27c", "P28"}


def points(value):
    return [tuple(map(float, p.split(","))) for p in (value or "").split()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", type=Path, default=SCENARIO / "bund.net.xml")
    parser.add_argument("--numbering", type=Path, default=SCENARIO / "patches/monument_m3.numbering.json")
    parser.add_argument("--output-directory", type=Path, default=ROOT / "outputs/road_network")
    args = parser.parse_args()
    network_bytes = args.network.read_bytes()
    sha = hashlib.sha256(network_bytes).hexdigest()
    root = ET.fromstring(network_bytes)
    edges = {e.get("id"): e for e in root.findall("edge")}
    nodes = {j.get("id"): j for j in root.findall("junction")}
    numbering = json.loads(args.numbering.read_text(encoding="utf-8"))
    labels = {j["label"]: j for j in numbering["junctions"]}
    road_labels = {r["label"]: r for r in numbering["roads"]}
    if (len(labels) != len(numbering["junctions"])
            or len(road_labels) != len(numbering["roads"])
            or len({j['sumo_junction_id'] for j in labels.values()}) != len(labels)
            or len({r['sumo_edge_id'] for r in road_labels.values()}) != len(road_labels)):
        raise ValueError("Duplicate diagram labels or SUMO IDs")
    node_labels = {j['sumo_junction_id']: j['label'] for j in labels.values()}
    for road in numbering["roads"]:
        edge = edges[road["sumo_edge_id"]]
        if (road["from_junction_id"], road["to_junction_id"]) != (edge.get("from"), edge.get("to")):
            raise ValueError(f"Stale road numbering: {road['label']}")
        if (road['from_label'], road['to_label']) != (node_labels[edge.get('from')], node_labels[edge.get('to')]):
            raise ValueError(f"Stale road endpoint labels: {road['label']}")
        if road['length_m'] != float(edge.find('lane').get('length')):
            raise ValueError(f"Stale road lane length: {road['label']}")
    for junction in numbering["junctions"]:
        node = nodes[junction["sumo_junction_id"]]
        if (junction["x_m"], junction["y_m"]) != (float(node.get("x")), float(node.get("y"))):
            raise ValueError(f"Stale junction position: {junction['label']}")
        degree = sum(junction['sumo_junction_id'] in (r['from_junction_id'], r['to_junction_id'])
                     for r in numbering['roads'])
        if degree != junction['scope_degree']:
            raise ValueError(f"Stale junction degree: {junction['label']}")
    context = sumolib.net.readNet(str(args.network))
    special = {road["sumo_edge_id"] for road in numbering["roads"]}

    def coord(node_id):
        node = nodes[node_id]
        return float(node.get("x")), float(node.get("y"))

    def full_shape(edge):
        shape = points(edge.get("shape") or edge.find("lane").get("shape"))
        return [coord(edge.get("from")), *shape, coord(edge.get("to"))]

    def intersects(shape, bounds):
        if not shape:
            return False
        xs, ys = zip(*shape)
        x0, x1, y0, y1 = bounds
        return min(xs) <= x1 and max(xs) >= x0 and min(ys) <= y1 and max(ys) >= y0

    plt.rcParams.update({"font.family": ["Hiragino Sans GB", "Arial Unicode MS", "sans-serif"],
                         "axes.unicode_minus": False, "font.size": 10, "svg.fonttype": "path"})
    fig = plt.figure(figsize=(22, 22), dpi=180, facecolor=BACKGROUND)
    overview = fig.add_axes([.045, .475, .19, .415])
    park = fig.add_axes([.275, .475, .315, .415])
    monument = fig.add_axes([.65, .695, .315, .195])
    north = fig.add_axes([.65, .54, .315, .115])
    p21 = fig.add_axes([.045, .26, .205, .18])
    additions = fig.add_axes([.285, .245, .205, .195])
    northeast = fig.add_axes([.525, .245, .205, .195])
    southeast = fig.add_axes([.765, .245, .205, .195])

    def draw(ax, bounds, title, scale, scale_on_right=False):
        ax.set_facecolor("white")
        for edge in context.getEdges():
            if edge.getID() in special or not edge.allows("pedestrian"):
                continue
            shape = edge.getShape()
            if intersects(shape, bounds):
                ax.plot(*zip(*shape), color="#e0e5ed", lw=.65, zorder=1)
        for road in numbering["roads"]:
            shape = full_shape(edges[road["sumo_edge_id"]])
            if not intersects(shape, bounds):
                continue
            dashed = road["label"].startswith(("S", "X"))
            ax.plot(*zip(*shape), color=road["color"], lw=2.6 if road["label"] in NEW_ROADS else 1.9,
                    linestyle="--" if dashed else "-", solid_capstyle="round", zorder=4 if road["label"] in NEW_ROADS else 2)
        for j in numbering["junctions"]:
            x, y = j["x_m"], j["y_m"]
            if bounds[0] <= x <= bounds[1] and bounds[2] <= y <= bounds[3]:
                selected = int(j["label"][1:]) >= 26
                ax.scatter(x, y, s=25 if selected else 16, facecolor="white" if selected else INK,
                           edgecolor="#32715f" if selected else "white", lw=1.3 if selected else .7, zorder=7)
        ax.set_xlim(bounds[:2]); ax.set_ylim(bounds[2:]); ax.set_aspect("equal", adjustable="box")
        ax.set_title(title, loc="left", color=INK, fontsize=13, pad=13)
        ax.set_xlabel("SUMO x（米）", color=MUTED, fontsize=9)
        ax.set_ylabel("SUMO y（米）", color=MUTED, fontsize=9)
        ax.grid(color="#edf0f5", lw=.6); ax.tick_params(colors=MUTED, labelsize=8)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=4, min_n_ticks=3, integer=True))
        ax.yaxis.set_major_locator(MaxNLocator(nbins=6, min_n_ticks=3, integer=True))
        for spine in ax.spines.values(): spine.set_color("#d4deeb")
        ax.annotate("北", xy=(.95, .91), xytext=(.95, .81), xycoords="axes fraction", ha="center", color=MUTED,
                    fontsize=9, arrowprops=dict(arrowstyle="->", color=MUTED, lw=1.2))
        x = bounds[1] - scale - .08 * (bounds[1] - bounds[0]) if scale_on_right else bounds[0] + .08 * (bounds[1] - bounds[0])
        y = bounds[2] + .06 * (bounds[3] - bounds[2])
        ax.plot([x, x + scale], [y, y], color=INK, lw=2, zorder=8)
        ax.text(x + scale / 2, y + .025 * (bounds[3] - bounds[2]), f"{scale} 米", ha="center", color=MUTED, fontsize=8, zorder=9)

    labelled_junctions = set()

    def label(ax, code, offset=None, position=None, fontsize=10):
        labelled_junctions.add(code)
        j = labels[code]; xy = (j["x_m"], j["y_m"])
        ax.annotate(code, xy, xytext=position if position is not None else offset or (8, 6),
                    textcoords="data" if position is not None else "offset points", fontsize=fontsize,
                    color=INK, ha="center" if position is not None else "left", va="center",
                    bbox=dict(boxstyle="round,pad=.2", fc="white", ec="#8b9aad", lw=.75, alpha=.97),
                    arrowprops=dict(arrowstyle="-", color="#8290a5", lw=.75), zorder=10)

    def road_label(ax, code, xy, fontsize=9):
        ax.text(*xy, code, fontsize=fontsize, color=road_labels[code]["color"],
                bbox=dict(fc="white", ec="none", alpha=.93), zorder=9)

    def walking_areas(ax, codes):
        for code in codes:
            edge = edges.get(f":{labels[code]['sumo_junction_id']}_w0")
            if edge is None:
                continue
            shape = points(edge.find("lane").get("shape"))
            if len(shape) >= 3:
                ax.add_patch(Polygon(shape, facecolor="#18a273", edgecolor="#18a273", alpha=.13, zorder=3))

    draw(overview, (18430, 18678, 5518, 6075), "整体范围与园外端点", 50)
    for code, offset in {"J01": (10, 0), "J22": (-32, 0), "J25": (10, -2)}.items(): label(overview, code, offset)
    overview.add_patch(Rectangle((18477, 5662), 183, 304, fill=False, ec="#8897ac", ls="--", lw=.8, zorder=5))
    draw(park, (18477, 18660, 5662, 5966), "公园道路与连接点", 50)
    park_offsets = {
        "J02": (-28, 13), "J03": (-6, 33), "J04": (25, 27), "J05": (27, -7),
        "J09": (8, 18), "J13": (-35, -3), "J15": (11, 11), "J16": (12, -9),
        "J17": (-34, 0), "J18": (12, 7), "J19": (12, -8), "J20": (-32, 3),
        "J21": (12, -5), "J23": (-31, -2), "J24": (12, -15), "J27": (-36, 3),
        "J28": (15, 16), "J29": (-37, 19), "J30": (-34, -8), "J31": (-37, -5), "J32": (-36, -2),
        "J33": (-39, 5), "J34": (12, -6), "J35": (14, 1), "J36": (14, 11),
        "J37": (12, -8),
    }
    for code, offset in park_offsets.items(): label(park, code, offset, fontsize=9.5)
    for code, xy in {"P04": (18538, 5904), "P07": (18607, 5784),
                     "P09": (18583, 5733), "P10": (18564, 5690),
                     "P12": (18536, 5695), "P13a": (18517, 5753),
                     "P14b": (18502, 5902), "P20a": (18515, 5900)}.items():
        road_label(park, code, xy, fontsize=8.5)
    park.add_patch(Rectangle((18569, 5813), 98, 81, fill=False, ec="#b2bdd0", ls="--", lw=.8, zorder=5))
    park.add_patch(Rectangle((18514, 5764), 58, 74, fill=False, ec="#94b8a7", ls="--", lw=.8, zorder=5))

    draw(monument, (18569, 18667, 5813, 5894), "纪念塔周边放大 · M3 / J26", 10)
    monument_positions = {"J06": (18652, 5886), "J07": (18652, 5877), "J08": (18589, 5880),
                          "J09": (18577, 5874), "J10": (18653, 5860), "J11": (18575, 5850),
                          "J12": (18653, 5833), "J13": (18576, 5837), "J14": (18598, 5828),
                          "J16": (18613, 5819), "J26": (18576, 5858)}
    for code, position in monument_positions.items(): label(monument, code, position=position, fontsize=10)
    for code, xy in {"M1": (18585, 5864), "M2": (18602, 5831), "M3": (18588, 5849),
                     "H1": (18634, 5886), "H2a": (18599, 5842), "H2b": (18591, 5855),
                     "H3": (18628, 5876), "H4": (18633, 5841), "H5": (18615, 5835),
                     "X1/X2": (18611, 5873), "X3": (18642, 5871), "X4": (18609, 5852)}.items():
        if code == "X1/X2":
            monument.text(*xy, code, fontsize=8, color=road_labels['X1']['color'],
                          bbox=dict(fc='white', ec='none', alpha=.93), zorder=9)
        else:
            road_label(monument, code, xy, fontsize=8.5)

    draw(additions, (18514, 18572, 5764, 5838), "P22 / P23 / P24 接入", 10, scale_on_right=True)
    walking_areas(additions, ("J29", "J30", "J31", "J32"))
    for code, offset in {"J17": (-32, 7), "J15": (7, 8), "J29": (-32, 13), "J30": (10, 4),
                         "J31": (8, 15), "J32": (-33, -16)}.items(): label(additions, code, offset, fontsize=9.5)
    for code, xy in {"P22": (18530, 5810), "P23": (18556, 5805), "P24": (18543, 5786),
                     "P13b": (18518, 5789), "P13a": (18536, 5766)}.items(): road_label(additions, code, xy)

    draw(north, (18486, 18533, 5907, 5935), "北门短段 · P01 / P02 / P03", 10)
    for code, position in {"J02": (18489, 5931), "J03": (18501, 5931), "J04": (18516, 5931),
                           "J05": (18528, 5909)}.items(): label(north, code, position=position)
    for code, xy in {"P01": (18494, 5923), "P02": (18505, 5918), "P03": (18513, 5913)}.items():
        road_label(north, code, xy, fontsize=9)
    draw(p21, (18499, 18538, 5859, 5888), "P21 两端接入", 10, scale_on_right=True)
    walking_areas(p21, ("J27", "J28"))
    label(p21, "J27", position=(18503, 5880)); label(p21, "J28", position=(18531, 5866))
    for code, xy in {"P21": (18517, 5871), "P14a": (18507, 5861), "P20a": (18521, 5884)}.items(): road_label(p21, code, xy)

    draw(northeast, (18514, 18591, 5807, 5888), "P25 / P28 · 东北支路", 10)
    walking_areas(northeast, ("J28", "J33", "J29", "J15"))
    for code, offset in {"J28": (-35, 7), "J33": (-35, 1), "J37": (9, 5),
                         "J29": (-35, -3), "J15": (10, -7), "J13": (5, 7)}.items():
        label(northeast, code, offset, fontsize=9.5)
    for code, xy in {"P25": (18531, 5840), "P28": (18550, 5855),
                     "P20b": (18532, 5870), "P20c": (18548, 5838),
                     "P15a": (18521, 5816), "P15b": (18542, 5826), "P16": (18572, 5834)}.items():
        road_label(northeast, code, xy, fontsize=9)

    draw(southeast, (18542, 18603, 5702, 5823), "P26 / P27 · 梯形折线", 10, scale_on_right=True)
    walking_areas(southeast, ("J21", "J34", "J30", "J35", "J36"))
    for code, offset in {"J21": (12, -3), "J34": (-34, 0), "J30": (-34, 8),
                         "J35": (8, 2), "J36": (8, 6), "J18": (9, -2), "J19": (9, -3)}.items():
        label(southeast, code, offset, fontsize=9.5)
    for code, xy in {"P26": (18567, 5738), "P27a": (18588, 5779),
                     "P27b": (18583, 5794), "P27c": (18568, 5814),
                     "P17b": (18557, 5785), "P17c": (18587, 5766)}.items():
        road_label(southeast, code, xy, fontsize=9)
    if labelled_junctions != set(labels):
        raise ValueError(f"Diagram omitted junction labels: {sorted(set(labels) - labelled_junctions)}")

    j34_width = float(edges[f":{labels['J34']['sumo_junction_id']}_w0"].find("lane").get("width"))
    j33_width = float(edges[f":{labels['J33']['sumo_junction_id']}_w0"].find("lane").get("width"))
    fig.text(.655, .515, "P20–P28 宽 2 米，可双向步行；绿色区域为实际行人路口。", fontsize=11, color=MUTED)
    fig.text(.655, .493, f"J33 为 {j33_width:g} 米，J34 为 {j34_width:g} 米，J15 为 6 米；J37 为尽端。", fontsize=11, color=MUTED)
    fig.text(.655, .471, "细灰线为周边道路背景；虚线 X 为场景排除路段。", fontsize=11, color=MUTED)

    fig.text(.045, .954, "黄浦公园与纪念塔 · 完整路网及路口编号图", fontsize=24, weight="bold", color=INK)
    fig.text(.045, .929, f"J01–J37 · {len(numbering['roads'])} 个道路分段 · 包含新增 M3、P20–P28 · 节点与边核对当前 SUMO 文件", fontsize=12, color=MUTED)
    fig.text(.045, .909, "保留原编号，更新配色；a、b、c 为道路分段。图形比例统一以米制坐标表示，前端使用同一源路网。", fontsize=11, color=MUTED)
    fig.text(.045, .208, "颜色—道路编号图例（括号内为两端路口；P27 的 a、b、c 是三段折线）", fontsize=12, color=INK)
    legend = fig.add_axes([.045, .038, .92, .156]); legend.set_axis_off()
    rows = 11
    for index, road in enumerate(numbering["roads"]):
        column, row = divmod(index, rows)
        x, y = column / 5, .945 - row * .085
        dashed = road["label"].startswith(("S", "X"))
        legend.plot([x, x + .026], [y, y], color=road["color"], lw=2.8,
                    ls="--" if dashed else "-", transform=legend.transAxes)
        legend.text(x + .036, y, f"{road['label']}（{road['from_label']}–{road['to_label']}）",
                    transform=legend.transAxes, va="center", fontsize=10.5, color="#4c5c74")
    fig.text(.045, .025, "P13 / P14 / P15 / P17 / P20 拆分后保留原编号；H2a、H2b 为同一短弧的两段，S1、S2 为历史园外路段。", fontsize=10, color=MUTED)
    fig.text(.045, .012, f"源路网 SHA-256：{sha}  ·  道路及路口 SUMO ID 对应关系见同名 numbering.json", fontsize=8, color="#8996a9")
    args.output_directory.mkdir(parents=True, exist_ok=True)
    stem = args.output_directory / "huangpu_park_road_network_complete"
    fig.savefig(stem.with_suffix(".png"), facecolor=BACKGROUND)
    fig.savefig(stem.with_suffix(".svg"), facecolor=BACKGROUND)
    snapshot = dict(numbering, source_network_sha256=sha, diagram=str(stem.with_suffix(".png").resolve()),
                    diagram_labelled_junctions=sorted(labelled_junctions))
    stem.with_suffix(".numbering.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    plt.close(fig)
    # A separate engineering detail keeps both distant new connectors readable
    # without compressing the full map's existing monument and north-gate views.
    detail = plt.figure(figsize=(16, 10), dpi=180, facecolor=BACKGROUND)
    left = detail.add_axes([.045, .185, .415, .655])
    right = detail.add_axes([.53, .185, .415, .655])
    draw(left, (18520, 18570, 5806, 5888), "P25 · J29 → J28–J15 中点 J33", 10)
    walking_areas(left, ("J29", "J33", "J28", "J15"))
    for code, offset in {"J28": (-39, 4), "J33": (17, 6), "J29": (-39, 9),
                         "J15": (15, -7), "J17": (-12, -16), "J30": (8, -6)}.items():
        label(left, code, offset, fontsize=10)
    label(left, "J37", (8, 1), fontsize=10)
    for code, xy in {"P25": (18531, 5840), "P20b": (18534, 5864), "P20c": (18550, 5840),
                     "P15a": (18524, 5816), "P15b": (18547, 5822)}.items(): road_label(left, code, xy, fontsize=10)
    road_label(left, "P28", (18550, 5860), fontsize=10)
    draw(right, (18540, 18604, 5702, 5818), "P26 · J21 → 近 J18 的三等分点 J34", 10, scale_on_right=True)
    walking_areas(right, ("J21", "J34", "J18", "J30", "J20"))
    for code, offset in {"J21": (20, 0), "J34": (-43, 4), "J18": (12, 1),
                         "J30": (12, 1), "J19": (14, -8), "J20": (-25, 10)}.items(): label(right, code, offset, fontsize=10)
    for code, xy in {"P26": (18570, 5748), "P17b": (18563, 5793), "P17c": (18586, 5763),
                     "P18": (18554, 5730), "P19": (18554, 5705), "P09": (18578, 5729)}.items(): road_label(right, code, xy, fontsize=10)
    detail.text(.045, .944, "黄浦公园 · P25 / P26 新增道路接入示意", fontsize=22, weight="bold", color=INK)
    detail.text(.045, .899, "按路口至路口的完整中心线长度取点；P17 保留原折点。绿色半透明区域为实际 walkingarea。", fontsize=11, color=MUTED)
    detail.text(.06, .125, "P25：33.20 米；J28–J33 与 J33–J15 等长。", fontsize=11, color=INK)
    detail.text(.53, .125, "P26：58.71 米；J18–J34 占 J18–J30 全长的 1/3。", fontsize=11, color=INK)
    detail.text(.06, .083, f"两条步道均宽 2 米；J29、J21、J33 为 4 米，J34 为 {j34_width:g} 米，J15 保持 6 米。", fontsize=11, color=MUTED)
    detail.text(.06, .044, "J29 为四岔，J21 为五岔；J34 加入 P27 后已重新检查并拓宽。", fontsize=11, color=MUTED)
    detail.text(.06, .012, f"源路网 SHA-256：{sha}", fontsize=8, color="#8996a9")
    detail_stem = args.output_directory / "huangpu_park_p25_p26_detail"
    detail.savefig(detail_stem.with_suffix(".png"), facecolor=BACKGROUND)
    detail.savefig(detail_stem.with_suffix(".svg"), facecolor=BACKGROUND)
    plt.close(detail)
    trapezoid = plt.figure(figsize=(16, 10), dpi=180, facecolor=BACKGROUND)
    direction_view = trapezoid.add_axes([.045, .215, .385, .63])
    geometry_view = trapezoid.add_axes([.505, .215, .45, .63])
    draw(direction_view, (18549, 18603, 5710, 5825), "P27 与 P26 的方向关系", 10, scale_on_right=True)
    draw(geometry_view, (18554, 18591, 5768, 5818), "近似等腰梯形放大 · 保留原折点", 5)
    for ax in (direction_view, geometry_view):
        walking_areas(ax, ("J30", "J34", "J35", "J36"))
        for code, offset in {"J30": (-35, 7), "J34": (9, -8), "J35": (13, -1), "J36": (10, 9)}.items():
            label(ax, code, offset, fontsize=10)
    label(direction_view, "J21", (10, -5)); label(direction_view, "J18", (10, -3))
    road_label(direction_view, "P26", (18570, 5748), fontsize=11)
    a = coord(labels["J34"]["sumo_junction_id"])
    b = coord(labels["J30"]["sumo_junction_id"])
    c = coord(labels["J35"]["sumo_junction_id"])
    d = coord(labels["J36"]["sumo_junction_id"])
    geometry_view.plot(*zip(a, b), ls="--", color="#77889b", lw=.9, zorder=3)
    base_mid = tuple((a[i]+b[i])/2 for i in (0,1))
    short_mid = tuple((c[i]+d[i])/2 for i in (0,1))
    geometry_view.annotate("", xy=short_mid, xytext=base_mid,
                           arrowprops=dict(arrowstyle="<->", color="#77889b", lw=1), zorder=9)
    geometry_view.text(18563, 5787, "长底边基准 45.18 米\n（J34–J30 端点连线）", fontsize=9,
                       color=MUTED, bbox=dict(fc="white", ec="none", alpha=.94), zorder=9)
    for code, xy in {"P27a": (18583.8, 5779), "P27b": (18582, 5798), "P27c": (18563, 5810)}.items():
        road_label(geometry_view, code, xy, fontsize=10)
    trapezoid.text(.045, .944, "黄浦公园 · P27 三段折线路网", fontsize=22, weight="bold", color=INK)
    trapezoid.text(.045, .899, "路线：J34 → J35 → J36 → J30；西南侧长底边保留原道路折点，东北侧短底边为基准长底边的一半。", fontsize=11, color=MUTED)
    trapezoid.text(.06, .151, "东南斜边 J34→J35 与 P26 的 J21→J34 平行且同向；由此计算高度约 8.38 米。", fontsize=11, color=INK)
    trapezoid.text(.06, .111, "短底边约 22.59 米，两条斜边各约 14.06 米，新增折线总长约 50.71 米。", fontsize=11, color=INK)
    trapezoid.text(.06, .072, f"步道均宽 2 米；J30、J35、J36 为 4 米，J34 为 {j34_width:g} 米；绿色区域为实际 walkingarea。", fontsize=11, color=MUTED)
    trapezoid.text(.06, .038, "新增及相邻共 8 个路口通过全转向行人压力测试；所有测试行人到达，连续停顿低于 15 秒。", fontsize=10, color=MUTED)
    trapezoid.text(.06, .009, f"源路网 SHA-256：{sha}", fontsize=8, color="#8996a9")
    trapezoid_stem = args.output_directory / "huangpu_park_p27_detail"
    trapezoid.savefig(trapezoid_stem.with_suffix(".png"), facecolor=BACKGROUND)
    trapezoid.savefig(trapezoid_stem.with_suffix(".svg"), facecolor=BACKGROUND)
    plt.close(trapezoid)
    perpendicular = plt.figure(figsize=(14, 10), dpi=180, facecolor=BACKGROUND)
    branch_view = perpendicular.add_axes([.065, .19, .66, .65])
    draw(branch_view, (18525, 18597, 5818, 5888), "P28 接入及长度参照", 10)
    walking_areas(branch_view, ("J33", "J28", "J15", "J13"))
    for code, offset in {"J28": (-40, 7), "J33": (-43, -2), "J37": (12, 4),
                         "J15": (13, -8), "J13": (12, 7)}.items(): label(branch_view, code, offset, fontsize=11)
    start = coord(labels["J33"]["sumo_junction_id"])
    endpoint = coord(labels["J37"]["sumo_junction_id"])
    j15 = coord(labels["J15"]["sumo_junction_id"])
    j28 = coord(labels["J28"]["sumo_junction_id"])
    import math
    direction = tuple((j28[i]-j15[i])/math.dist(j15,j28) for i in (0,1))
    outward = tuple((endpoint[i]-start[i])/math.dist(start,endpoint) for i in (0,1))
    corner = [tuple(start[i]+2*direction[i] for i in (0,1)),
              tuple(start[i]+2*direction[i]+2*outward[i] for i in (0,1)),
              tuple(start[i]+2*outward[i] for i in (0,1))]
    branch_view.plot(*zip(*corner), color=INK, lw=1, zorder=9)
    branch_view.text(start[0]+1.7, start[1]+4, "90°", fontsize=10, color=INK, zorder=9)
    road_label(branch_view, "P28", (18549, 5855), fontsize=11)
    road_label(branch_view, "P20b", (18532, 5871), fontsize=10)
    road_label(branch_view, "P20c", (18547, 5838), fontsize=10)
    road_label(branch_view, "P25", (18530, 5840), fontsize=10)
    branch_view.text(18569, 5834, "P16：J15–J13\n完整长度约 34.51 米", fontsize=10,
                     color=MUTED, bbox=dict(fc="white", ec="none", alpha=.94), zorder=9)
    perpendicular.text(.065, .944, "黄浦公园 · P28 东北垂直支路", fontsize=22, weight="bold", color=INK)
    perpendicular.text(.065, .899, "由 J33 向东北引出，与 J15–J28 垂直；长度按完整道路中心线计算，为 J15–J13 的一半。", fontsize=11, color=MUTED)
    perpendicular.text(.78, .765, "P28：J33 → J37", fontsize=13, color=INK)
    perpendicular.text(.78, .705, "完整长度约 17.25 米", fontsize=11, color=MUTED)
    perpendicular.text(.78, .655, "步道宽 2 米", fontsize=11, color=MUTED)
    perpendicular.text(.78, .605, f"J33 walkingarea：{j33_width:g} 米", fontsize=11, color=MUTED)
    perpendicular.text(.78, .555, "J37：自由尽端", fontsize=11, color=MUTED)
    perpendicular.text(.78, .475, "绿色区域为实际\nwalkingarea 几何", fontsize=10, color=MUTED)
    perpendicular.text(.065, .12, "J33 已重建为四岔路口；接入路口及相邻共 4 个路口通过全转向压力测试。", fontsize=11, color=INK)
    perpendicular.text(.065, .073, "18 名行人完成进入 P28、接近 J37、折返并离开的测试；新增道路无其他相交点。", fontsize=11, color=MUTED)
    perpendicular.text(.065, .021, f"源路网 SHA-256：{sha}", fontsize=8, color="#8996a9")
    perpendicular_stem = args.output_directory / "huangpu_park_p28_detail"
    perpendicular.savefig(perpendicular_stem.with_suffix(".png"), facecolor=BACKGROUND)
    perpendicular.savefig(perpendicular_stem.with_suffix(".svg"), facecolor=BACKGROUND)
    plt.close(perpendicular)
    print(json.dumps({"png": str(stem.with_suffix('.png').resolve()), "svg": str(stem.with_suffix('.svg').resolve()),
                      "detail_png": str(detail_stem.with_suffix('.png').resolve()),
                      "trapezoid_png": str(trapezoid_stem.with_suffix('.png').resolve()),
                      "perpendicular_png": str(perpendicular_stem.with_suffix('.png').resolve()),
                      "roads": len(numbering['roads']), "junctions": len(labels), "network_sha256": sha}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
