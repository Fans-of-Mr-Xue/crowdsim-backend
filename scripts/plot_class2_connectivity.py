"""Plot study-area roads colored by the actual SUMO pedestrian components."""
import json
from collections import defaultdict
from pathlib import Path
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Polygon, Rectangle

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.decision.position_aware_router import PositionAwarePedestrianRouter
from crowdsim.scenarios.class2_flow_layout import flow_network

network = ResearchNetwork(str(ROOT/'scenarios/shanghai_bund/bund.net.xml'))
router = PositionAwarePedestrianRouter(network)
data = flow_network()
groups = {}
for node in router.adjacency:
    if node in groups:
        continue
    group = len(groups)
    pending = [node]; groups[node] = group
    while pending:
        for arc in router.adjacency[pending.pop()]:
            if arc.to not in groups:
                groups[arc.to] = group
                pending.append(arc.to)
lanes_by_group = defaultdict(list)
for lane in data['lanes']:
    lanes_by_group[groups[(lane['edgeId'], 0)]].append(lane)
ordered = sorted(lanes_by_group, key=lambda group: -len(lanes_by_group[group]))
assert [len(lanes_by_group[group]) for group in ordered] == [50, 1]
palette = dict(zip(ordered, ['#2167b0', '#dc493a']))
origin = data['originXY']
plt.rcParams.update({'font.family': ['Hiragino Sans GB', 'Arial Unicode MS', 'sans-serif'],
                     'axes.unicode_minus': False, 'svg.fonttype': 'path', 'font.size': 11})
fig = plt.figure(figsize=(13.6, 9.0), dpi=180, facecolor='#fbfcfe')
fig.text(.055, .95, '纪念塔—黄浦公园 · 步行路网连通示意', fontsize=23, weight='bold', color='#142d45')
fig.text(.055, .908, '依据当前 SUMO 步行连接关系分组；颜色表示连通部分，不表示拥挤程度', fontsize=11, color='#597083')
overview = fig.add_axes([.055, .12, .40, .72])
detail = fig.add_axes([.53, .23, .42, .61])
bounds = (19, 105, 15, 125)

def configure(ax, limits, title):
    ax.set_facecolor('#fbfcfe')
    ax.add_patch(Polygon(data['scope'], closed=True, facecolor='#edf1f4',
                         edgecolor='#99a9b6', linestyle=(0, (4, 4)), linewidth=1.1, zorder=0))
    for lane in data['lanes']:
        group = groups[(lane['edgeId'], 0)]
        xs, ys = zip(*lane['path'])
        ax.plot(xs, ys, color=palette[group], linewidth=2.6 if group==ordered[0] else 4,
                solid_capstyle='round', zorder=3)
    ax.set_xlim(*limits[:2]); ax.set_ylim(*limits[2:])
    ax.set_aspect('equal')
    ax.set_title(title, loc='left', fontsize=13, color='#142d45', pad=14, weight='bold')
    ax.set_xlabel('局部东西坐标 / 米', color='#597083', fontsize=10)
    ax.set_ylabel('局部南北坐标 / 米', color='#597083', fontsize=10)
    ax.tick_params(colors='#7a8b98', labelsize=9)
    ax.grid(True, color='#dae1e8', linewidth=.45, zorder=-1)
    for spine in ax.spines.values(): spine.set_color('#d4dfe7')

configure(overview, (-10, 165, -10, 270), 'A  研究范围总览')
configure(detail, bounds, 'B  孤立步道及其周边放大')
# Show the full small component, including its outside-scope continuation.
small = ordered[1]
small_edges = []
for eid, edge in network.edges.items():
    if edge.getFunction() or groups.get((eid, 0)) != small:
        continue
    small_edges.append(eid)
    shape = [(x-origin[0], y-origin[1]) for x,y in edge.getShape()]
    xs, ys = zip(*shape)
    detail.plot(xs, ys, color=palette[small], lw=2.4, linestyle=(0,(3,2)), zorder=2)
for eid, edge in network.edges.items():
    if edge.getFunction() != 'walkingarea' or groups.get((eid, 0)) != small:
        continue
    shape = [(x-origin[0], y-origin[1]) for x,y in edge.getShape()]
    if len(shape) >= 3:
        detail.add_patch(Polygon(shape, color=palette[small], alpha=.18, zorder=2))
# Draw the in-scope red portion over dashed context.
isolated = lanes_by_group[small][0]
xs, ys = zip(*isolated['path'])
detail.plot(xs, ys, color=palette[small], linewidth=4, solid_capstyle='round', zorder=4)
cx, cy = sum(xs)/len(xs), sum(ys)/len(ys)
overview.add_patch(Rectangle((bounds[0],bounds[2]), bounds[1]-bounds[0], bounds[3]-bounds[2],
                            fill=False, edgecolor='#5d7488', lw=1.1, linestyle='--', zorder=5))
overview.annotate('B 放大区域', (bounds[1],bounds[3]), xytext=(105,137), color='#597083', fontsize=10)
overview.scatter([cx],[cy],s=180,facecolor='none',edgecolor=palette[small],lw=1.4,zorder=6)
overview.annotate('孤立短步道', (cx,cy), xytext=(84,24), fontsize=11, color=palette[small],
                  arrowprops={'arrowstyle':'->','color':palette[small],'lw':1.1})
detail.annotate('范围内的孤立短步道\n178564467 · 13.65 米', (cx,cy), xytext=(59,73),
                color=palette[small], fontsize=10.5, va='center',
                bbox={'boxstyle':'round,pad=.4','fc':'#fff8f5','ec':'none'},
                arrowprops={'arrowstyle':'->','color':palette[small],'lw':1.2})
for eid, offset, label in [('178564462#0',(3,5),'178564462#0'),('178564462#1',(3,0),'178564462#1')]:
    edge=network.edges[eid]
    shape=edge.getShape()
    xy=shape[len(shape)//2]; x,y=xy[0]-origin[0],xy[1]-origin[1]
    detail.text(x+offset[0],y+offset[1],label,color=palette[small],fontsize=9)
overview.annotate('N', xy=(147,255), xytext=(147,234), ha='center', color='#597083',
                  arrowprops={'arrowstyle':'-|>','color':'#597083'}, fontsize=11)
fig.legend(handles=[
    Line2D([0],[0],color=palette[ordered[0]],lw=3,label='蓝色：主连通部分（范围内 50 条）'),
    Line2D([0],[0],color=palette[small],lw=3,label='红色：另一连通部分（范围内 1 条）'),
    Line2D([0],[0],color=palette[small],lw=2,linestyle='--',label='红色虚线：该部分在研究范围外的延伸'),
], loc='lower left',bbox_to_anchor=(.52,.07),frameon=False,fontsize=10,labelspacing=.85)
fig.text(.055,.035,'灰色底纹及虚线边界为研究范围。红色部分在完整路网中包含 3 段步道，与蓝色主路网没有步行连接。',fontsize=10,color='#597083')
out = ROOT/'outputs/road_network'
out.mkdir(parents=True,exist_ok=True)
for ext in ('png','svg'):
    fig.savefig(out/f'class2_connectivity.{ext}',facecolor=fig.get_facecolor())
(out/'class2_connectivity.json').write_text(json.dumps({
    'networkSha256':data['sourceSha256'],
    'components':[{'color':palette[group],'studyEdgeCount':len(lanes_by_group[group]),
                   'studyEdgeIds':[lane['edgeId'] for lane in lanes_by_group[group]]} for group in ordered],
    'isolatedFullComponentEdges':sorted(small_edges)
},ensure_ascii=False,indent=2)+'\n')
print(out/'class2_connectivity.png')
