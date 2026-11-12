"""Static reconstructions of class-one archived positions, never live map screenshots."""
from pathlib import Path
import hashlib
import math
import xml.etree.ElementTree as ET


def render_images(output, geometry, manifest, moments, frames, *, title, round_number):
    # Optional plotting dependencies are loaded only when class-one pictures are requested.
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.colors import LinearSegmentedColormap
    from matplotlib.lines import Line2D
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path as PlotPath
    import numpy as np
    from shapely.geometry import shape, box, Polygon, LineString
    from shapely.ops import unary_union
    import sumolib

    root = Path(__file__).resolve().parents[2]
    network = root / 'scenarios/shanghai_bund/bund.net.xml'
    if hashlib.sha256(network.read_bytes()).hexdigest() != geometry['network_sha256']:
        raise ValueError('原始路网版本已变化，不能用新路网重绘旧实验。')
    scope = shape(geometry['scope_geometry'])
    xmin, ymin, xmax, ymax = scope.bounds
    bounds = (xmin - 15, ymin - 15, xmax + 15, ymax + 15)
    viewport = box(*bounds)
    net = sumolib.net.readNet(str(network), withInternal=True, withPedestrianConnections=True)
    walkways, background = [], []
    for edge in net.getEdges(withInternal=True):
        for lane in edge.getLanes():
            points = lane.getShape()
            if len(points) < 2 or not LineString(points).intersects(viewport):
                continue
            polygon = LineString(points).buffer(max(.4, lane.getWidth()/2), cap_style=2).intersection(viewport)
            (walkways if lane.allows('pedestrian') else background).append(polygon)
    walkways, background = unary_union(walkways), unary_union(background)
    nodes, monument = {}, None
    for _, item in ET.iterparse(root / 'scenarios/shanghai_bund/bund_real.osm.xml', events=('end',)):
        if item.tag == 'node':
            nodes[item.get('id')] = (float(item.get('lon')), float(item.get('lat')))
        elif item.tag == 'way' and item.get('id') == '47007059':
            monument = Polygon([net.convertLonLat2XY(*nodes[n.get('ref')]) for n in item.findall('nd')])

    colors = {'normal': '#2ecc71', 'busy': '#eab308', 'planned_stop': '#3b82f6', 'crowded': '#f97316', 'high_risk': '#ef4444'}
    labels = {'normal': '正常通行', 'busy': '繁忙或开始受阻', 'planned_stop': '计划内停留', 'crowded': '明显拥挤', 'high_risk': '高风险拥挤'}
    cmap = LinearSegmentedColormap.from_list('crowdsim_heat', [(0, '#3b82f6'), (.25, '#22d3ee'),
        (.45, '#22c55e'), (.65, '#facc15'), (.82, '#f97316'), (1, '#ef4444')])

    def patch(ax, geom, face, edge='none', width=.5, zorder=1, linestyle='solid'):
        if geom.is_empty:
            return
        if geom.geom_type in ('MultiPolygon', 'GeometryCollection'):
            for part in geom.geoms:
                patch(ax, part, face, edge, width, zorder, linestyle)
        elif geom.geom_type == 'Polygon':
            vertices, codes = [], []
            for ring in [geom.exterior, *geom.interiors]:
                points = list(ring.coords)
                vertices += points
                codes += [PlotPath.MOVETO] + [PlotPath.LINETO]*(len(points)-2) + [PlotPath.CLOSEPOLY]
            ax.add_patch(PathPatch(PlotPath(vertices, codes), facecolor=face, edgecolor=edge,
                                  linewidth=width, linestyle=linestyle, zorder=zorder))

    def base(ax):
        ax.set_facecolor('#f6f8fb')
        patch(ax, scope, '#edf3e9', zorder=0)
        patch(ax, background, '#e2e6eb', '#d2d9e0', .25, 1)
        patch(ax, walkways, '#d9e3e9', '#bccbd5', .35, 2)
        if monument is not None:
            patch(ax, monument, '#d1d9e3', '#7b8ba1', .7, 3)
            c = monument.centroid
            ax.text(c.x, c.y, '纪念塔', ha='center', va='center', color='#34465e', fontsize=9, zorder=9,
                    bbox={'facecolor': 'white', 'edgecolor': 'none', 'alpha': .82, 'pad': 2})
        patch(ax, scope, 'none', '#5b8f91', .9, 8, (0, (4, 3)))
        ax.set_xlim(bounds[0], bounds[2]); ax.set_ylim(bounds[1], bounds[3]); ax.set_aspect('equal')
        ax.set_xticks([]); ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_visible(False)
        x, y = bounds[2]-12, bounds[3]-18
        ax.annotate('', (x, y+8), (x, y-3), arrowprops={'arrowstyle': '-|>', 'color': '#516279', 'lw': .9}, zorder=10)
        ax.text(x, y+11, '北', ha='center', color='#516279', fontsize=9, zorder=10)
        x, y = bounds[0]+11, bounds[1]+10
        ax.plot([x, x+25], [y, y], color='#516279', lw=1.7, zorder=10)
        ax.text(x+12.5, y+3.3, '25 米', ha='center', color='#516279', fontsize=8, zorder=10)

    # Same equal-person, fixed 40-person scale / 20m support as the accepted preview.
    # The offline blurred-disc kernel approximates browser Canvas heat rendering.
    pixel = .5
    xs, ys = [np.arange(bounds[a], bounds[a+2]+pixel, pixel) for a in (0, 1)]
    axis = np.arange(-20, 20+pixel, pixel)
    kx, ky = np.meshgrid(axis, axis)
    kernel = ((kx*kx + ky*ky) <= 12*12).astype(float)
    weights = np.exp(-.5*(np.arange(-16, 17)/8)**2)
    weights /= weights.sum()
    for dimension in (0, 1):
        kernel = np.apply_along_axis(lambda line: np.convolve(line, weights, mode='same'), dimension, kernel)

    def heat(rows):
        buckets = {}
        for p in rows:
            x, y = float(p['x']), float(p['y'])
            group = buckets.setdefault((math.floor((x-bounds[0])/10), math.floor((y-bounds[1])/10)), [0., 0., 0])
            group[0] += x; group[1] += y; group[2] += 1
        transmission = np.ones((len(ys), len(xs)))
        radius = len(axis)//2
        for sx, sy, n in buckets.values():
            col, row = round((sx/n-bounds[0])/pixel), round((sy/n-bounds[1])/pixel)
            x0, x1 = max(0, col-radius), min(len(xs), col+radius+1)
            y0, y1 = max(0, row-radius), min(len(ys), row+radius+1)
            if x0 < x1 and y0 < y1:
                transmission[y0:y1, x0:x1] *= 1-min(n/40, 1)*kernel[y0-row+radius:y1-row+radius, x0-col+radius:x1-col+radius]
        field = 1-transmission
        rgba = cmap(field); rgba[:, :, 3] = field*.95
        return rgba

    available = {font.name for font in font_manager.fontManager.ttflist}
    font = next((f for f in ['Arial Unicode MS', 'PingFang SC', 'Noto Sans CJK SC', 'Microsoft YaHei', 'SimHei'] if f in available), 'DejaVu Sans')
    moment_labels = {'start': '开始', 'middle': '中间', 'end': '结束'}
    used = {p['visual_state'] for values in frames.values() for p in values.values()}
    legend = [Line2D([0], [0], marker='o', linestyle='', markerfacecolor=color, markeredgecolor='white', markersize=7, label=labels[s])
              for s, color in colors.items() if s in used]
    with plt.rc_context({'font.family': font, 'axes.unicode_minus': False, 'font.size': 12}):
        for moment in moments:
            if not moment['images']:
                continue
            rows = list(frames[moment['snapshotId']].values())
            for kind, filename in moment['images'].items():
                fig = plt.figure(figsize=(7, 10), dpi=180)
                try:
                    name = '场景推演' if kind == 'scene' else '人流热力图'
                    fig.text(.5, .96, f'{name} · {moment_labels[moment["key"]]}', ha='center', fontsize=21, weight='bold', color='#17304d')
                    fig.text(.5, .923, f'{title} · 第 {round_number} 轮 | T+{moment["timeSeconds"]:g} 秒 | {len(rows)} 人', ha='center', fontsize=11, color='#62748a')
                    ax = fig.add_axes([.08, .105, .84, .79]); base(ax)
                    if kind == 'scene':
                        for state, color in colors.items():
                            selected = [p for p in rows if p['visual_state'] == state]
                            ax.scatter([float(p['x']) for p in selected], [float(p['y']) for p in selected], s=9, c=color,
                                       alpha=.93, edgecolors='white', linewidths=.12, zorder=5)
                        fig.legend(handles=legend, loc='lower center', bbox_to_anchor=(.5, .035), ncol=2, frameon=False, fontsize=10)
                    else:
                        ax.imshow(heat(rows), origin='lower', extent=(xs[0], xs[-1], ys[0], ys[-1]), interpolation='bilinear', zorder=5)
                        bar = fig.add_axes([.26, .058, .48, .013])
                        bar.imshow(np.linspace(0, 1, 256)[None, :], aspect='auto', cmap=cmap, extent=(0, 1, 0, 1))
                        bar.set_yticks([]); bar.set_xticks([0, 1], ['低', '高']); bar.tick_params(length=0, labelsize=10)
                        bar.set_title('聚集强度', fontsize=10, color='#52647a', pad=6)
                        for spine in bar.spines.values(): spine.set_visible(False)
                    fig.savefig(Path(output)/filename, dpi=180, facecolor='white')
                finally:
                    plt.close(fig)
