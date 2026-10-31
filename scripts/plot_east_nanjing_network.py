"""Draw the installed SUMO network, including actual walking-area polygons."""
from pathlib import Path
import csv
import json
import os
import xml.etree.ElementTree as ET

os.environ.setdefault("MPLCONFIGDIR", "/private/tmp/crowdsim-mpl-cache")
os.environ.setdefault("XDG_CACHE_HOME", "/private/tmp/crowdsim-font-cache")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Polygon as PolygonPatch
from matplotlib.ticker import MultipleLocator
import sumolib

SCENE = Path(__file__).resolve().parents[1] / "scenarios/east_nanjing_road"
INK, MUTED, BG = "#192538", "#65758b", "#f7f9fc"


def main():
    font = Path("/Library/Fonts/Arial Unicode.ttf")
    if font.exists():
        font_manager.fontManager.addfont(str(font))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(font)).get_name()
    plt.rcParams.update({"axes.unicode_minus": False, "svg.fonttype": "path"})
    spec = json.loads((SCENE / "network_spec.json").read_text())
    manifest = json.loads((SCENE / "build_manifest.json").read_text())
    root = ET.parse(SCENE / "east_nanjing.net.xml").getroot()
    net = sumolib.net.readNet(str(SCENE / "east_nanjing.net.xml"), withInternal=True, withPedestrianConnections=True)
    nodes = {node.get("id"): [float(node.get("x")), float(node.get("y"))]
             for node in root.findall("junction") if node.get("type") != "internal"}
    edge_xml = {edge.get("id"): edge for edge in root.findall("edge")}
    widths = {item["junction"]: item["width_m"] for item in manifest["walking_areas"]}
    shape = lambda text: [tuple(map(float, p.split(","))) for p in text.split()]
    boundary = [net.convertLonLat2XY(*p) for p in spec["preset_wgs84_coordinates"]]
    fig = plt.figure(figsize=(18, 12), facecolor=BG)
    fig.text(.045, .966, "南京东路外滩路口 · 已生成 SUMO 路网", fontsize=23, color=INK, va="top")
    fig.text(.045, .926, "31 条双向可步行道路，24 个节点，23 个 walking area；J20 为单路端点。南侧终止于 J30 / J31，由 R31 连接。", fontsize=10.5, color=MUTED)
    fig.text(.045, .901, "路口宽度：18 处为 4 米；J11、J15 为 5 米；J01、J03、J19 保留 6.4 米。浅蓝区域为 SUMO 实际 walking-area 形状。", fontsize=10.5, color=MUTED)
    full = fig.add_axes([.045, .32, .43, .52])
    junction = fig.add_axes([.54, .59, .425, .25])
    south = fig.add_axes([.56, .32, .225, .20])
    middle = fig.add_axes([.82, .37, .145, .12])

    def panel(ax, bounds, title, interval, scale):
        x0, x1, y0, y1 = bounds
        ax.set_facecolor("white")
        ax.fill(*zip(*boundary), color="#dfeaf5", alpha=.2, zorder=1)
        closed = boundary + [boundary[0]]
        ax.plot(*zip(*closed), color="#7893b2", lw=.9, ls=(0, (2, 3)), zorder=2)
        for edge in root.findall("edge[@function='walkingarea']"):
            points = shape(edge.find("lane").get("shape"))
            if len(points) >= 3:
                ax.add_patch(PolygonPatch(points, facecolor="#79bdd5", edgecolor="#3397b8", alpha=.28, lw=.5, zorder=3))
        for road in spec["roads"]:
            edge = edge_xml[road["id"]]
            points = shape(edge.get("shape")) if edge.get("shape") else [nodes[road["from"]], nodes[road["to"]]]
            ax.plot(*zip(*points), color=road["color"], lw=2, zorder=4)
        ax.set(xlim=(x0, x1), ylim=(y0, y1), xlabel="SUMO x（米）", ylabel="SUMO y（米）")
        ax.set_aspect("equal", adjustable="box")
        ax.set_title(title, fontsize=12, color=INK, pad=10)
        ax.xaxis.set_major_locator(MultipleLocator(interval))
        ax.yaxis.set_major_locator(MultipleLocator(interval))
        ax.tick_params(labelsize=7, colors="#7c8da4", length=2.5)
        ax.xaxis.label.set(color="#7c8da4", fontsize=7)
        ax.yaxis.label.set(color="#7c8da4", fontsize=7)
        ax.grid(color="#e9eef5", lw=.5)
        for spine in ax.spines.values():
            spine.set_color("#ccd8e7")
        sx, sy = x0 + .07*(x1-x0), y0 + .06*(y1-y0)
        ax.plot([sx, sx+scale], [sy, sy], color=INK, lw=2)
        ax.text(sx+scale/2, sy+.016*(y1-y0), f"{scale} 米", fontsize=7, color=MUTED, ha="center")
        for label, xy in nodes.items():
            if x0 <= xy[0] <= x1 and y0 <= xy[1] <= y1:
                ax.plot(*xy, "o", ms=3.4, color=INK, markeredgecolor="white", markeredgewidth=.5, zorder=7)

    def labels(ax, positions, width_text=False):
        for label, xy in positions.items():
            text = label + (f" · {widths[label]:g}m" if width_text and label in widths else "")
            ax.annotate(text, xy=nodes[label], xytext=xy, ha="center", va="center", fontsize=7.5, color=INK,
                        bbox={"boxstyle": "round,pad=.15", "fc": "white", "ec": "#7f92ac", "lw": .7},
                        arrowprops={"arrowstyle": "-", "color": "#7f92ac", "lw": .7}, zorder=8)

    panel(full, (18220, 18596, 5255, 5725), "整体路网与预设区域边界", 100, 50)
    panel(junction, (18428, 18563, 5450, 5535), "道路接入与四向路口", 20, 10)
    panel(south, (18510, 18567, 5271, 5310), "南侧连接：J30—R31—J31", 10, 10)
    panel(middle, (18448, 18463, 5485.4, 5491.1), "J07 / J08 独立短连接", 5, 2)
    labels(full, {"J01": (18241, 5483), "J02": (18283, 5469), "J03": (18273, 5396), "J04": (18339, 5424),
        "J17": (18514, 5705), "J18": (18579, 5705), "J19": (18502, 5657), "J20": (18575, 5656),
        "J11": (18488, 5531), "J15": (18484, 5441), "J28": (18578, 5520), "J29": (18578, 5461),
        "J30": (18505, 5277), "J31": (18577, 5305)})
    labels(junction, {"J06": (18444, 5527), "J09": (18470, 5527), "J10": (18489, 5527),
        "J11": (18510, 5527), "J28": (18548, 5527), "J26": (18438, 5507),
        "J07": (18470, 5494), "J08": (18438, 5487), "J27": (18438, 5460),
        "J12": (18459, 5454), "J13": (18476, 5454), "J14": (18492, 5454), "J15": (18510, 5454), "J29": (18548, 5454)})
    labels(south, {"J30": (18522, 5302), "J31": (18556, 5276)}, True)
    labels(middle, {"J08": (18451, 5489.8), "J07": (18460, 5489.8)})
    junction.text(18524, 5515, "R29", fontsize=8, color="#b86505", ha="center")
    junction.text(18524, 5480, "R30", fontsize=8, color="#147c76", ha="center")
    south.text(18539, 5289, "R31", fontsize=8.5, color="#ad4fa1", ha="center", rotation=-18)
    legend = fig.add_axes([.045, .065, .92, .185])
    legend.set(xlim=(0, 1), ylim=(0, 1), xticks=[], yticks=[], facecolor="white")
    for spine in legend.spines.values():
        spine.set_color("#ccd8e7")
    legend.text(.017, .935, "道路编号（与 SUMO edge ID 一致）及连接路口", fontsize=11, color=INK)
    for col in range(4):
        left = .017 + .245*col
        for row, road in enumerate(spec["roads"][col*8:(col+1)*8]):
            y = .80 - .095*row
            legend.plot([left, left+.032], [y, y], color=road["color"], lw=2.5)
            legend.text(left+.043, y, f'{road["id"]}  ({road["from"]}—{road["to"]})', fontsize=8.5, color="#475b75", va="center")
    fig.text(.045, .028, "绘图读取已生成的 east_nanjing.net.xml。walking-area 的 width 是 SUMO 行人模型参数，其形状由 netconvert 生成；宽度规则已固化到生成脚本。", fontsize=8.2, color=MUTED)
    for extension in ["png", "svg"]:
        fig.savefig(SCENE / f"east_nanjing_network.{extension}", dpi=240, facecolor=BG)
    plt.close(fig)
    with (SCENE / "junction_widths.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["路口", "walking area ID", "宽度m", "最低要求m", "连接道路"])
        for item in manifest["walking_areas"]:
            writer.writerow([item["junction"], item["walking_area"], item["width_m"], item["minimum_m"], " | ".join(item["incident_roads"])])
    print(SCENE / "east_nanjing_network.png")


if __name__ == "__main__":
    main()
