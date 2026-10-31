"""Metric graph cells derived from saved boundary and environment road geometry."""
import heapq
import math
from collections import deque
import numpy as np
from pyproj import CRS, Transformer
from shapely.geometry import LineString, Point, box, mapping, shape
from shapely.ops import transform, unary_union
from ..services.pedestrian_network import surface_feature, pieces


def build_network(task, roads, settings):
    boundary = shape(task['region']['geometry'])
    if not boundary.is_valid:
        raise ValueError('任务边界存在自相交，请先修正边界')
    lon, lat = boundary.centroid.coords[0]
    metric = CRS.from_proj4(f'+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m')
    forward = Transformer.from_crs('EPSG:4326', metric, always_xy=True).transform
    backward = Transformer.from_crs(metric, 'EPSG:4326', always_xy=True).transform
    polygon = transform(forward, boundary)
    if not 4 <= polygon.area <= 2000000:
        raise ValueError('请选择 4 ㎡至 2 平方公里的局部区域进行人群聚集预测')
    warnings, paths, recorded_widths = list(roads.get('warnings', [])), [], []
    simulation_widths = 0
    lower_levels = 0
    for feature in roads.get('features', []):
        props = feature.get('properties') or {}
        if not surface_feature(feature):
            lower_levels += 1
            continue
        if props.get('access') in ('no', 'private') or props.get('foot') == 'no':
            continue
        if props.get('highway') not in ('footway', 'path', 'pedestrian', 'steps', 'living_street') and props.get('foot') not in ('yes', 'designated'):
            continue
        geometry = feature.get('geometry') or {}
        if geometry.get('type') not in ('LineString', 'MultiLineString'):
            continue
        try:
            width = float(str(props.get('width', settings['path_width_m'])).replace('m', '').strip())
            if not math.isfinite(width) or width <= 0:
                raise ValueError('invalid road width')
            width_available = props.get('width') is not None
        except (ValueError, TypeError):
            width = settings['path_width_m']
            width_available = False
        width = min(30, max(1, width))
        if width_available:
            recorded_widths.append(width)
            simulation_widths += props.get('width_source') == 'sumo_simulation_parameter'
        path = transform(forward, shape(geometry)).buffer(width / 2, cap_style=2).intersection(polygon)
        if not path.is_empty:
            paths.append(path)
    missing_widths = len(recorded_widths) < len(paths)
    for feature in roads.get('walkable_areas', {}).get('features', []):
        if surface_feature(feature):
            area = transform(forward, shape(feature['geometry'])).intersection(polygon)
            if not area.is_empty:
                paths.append(area)
    if settings['allow_boundary_walkable']:
        walkable = polygon
        warnings.append('本次明确将任务边界内部假设为可行走空间；未识别草地、建筑、围栏及其他障碍。')
    elif paths:
        walkable = unary_union(paths)
        if roads.get('simulation_network'):
            # SUMO's centimetre coordinates become millimetre gaps between
            # buffered lane endpoints and exported walking-area polygons.
            # Close <= 1 cm cracks BEFORE removing explicit barriers/obstacles.
            walkable = walkable.buffer(.005, join_style=2).buffer(-.005, join_style=2).intersection(polygon)
            warnings.append('SUMO 步道与路口面按 1 厘米数值容差闭合导出缝隙；真实障碍仍在闭合后扣除，不补接米级断路。')
        warnings.append('可行走空间来自 SUMO 道路宽度与路口步行区域；沿用当前热点的通行设置。' if roads.get('simulation_network') else '可行走空间来自环境库步行道路缓冲区；缺失的广场、入口和障碍物需要补测。')
    else:
        raise ValueError('任务范围内没有可用步行路网；请补充环境库路网，或明确勾选区域内部可行走假设')
    if not settings['allow_boundary_walkable']:
        obstacles = [transform(forward, shape(feature['geometry'])) for feature in roads.get('obstacles', {}).get('features', []) if surface_feature(feature)]
        # Only explicit physical barriers are removed; their small thickness is
        # a numerical separation tolerance, not a measured wall width.
        barriers = [transform(forward, shape(feature['geometry'])).buffer(.05) for feature in roads.get('barriers', {}).get('features', [])
                    if surface_feature(feature) and feature.get('properties', {}).get('barrier') in ('wall', 'fence', 'hedge')]
        if obstacles or barriers:
            walkable = walkable.difference(unary_union(obstacles + barriers))
            warnings.append('已排除公开地图记录的地面建筑、种植区、水面和实体围栏；未收录设施仍需核对。')
    if lower_levels:
        warnings.append(f'{lower_levels} 条下沉层/非地面通道保留在环境库，本次地面二维计算不混入；尚未实现跨层人流求解。')
    if walkable.is_empty:
        raise ValueError('排除已知障碍后没有可计算的地面步行空间，请核对楼层与场地资料')
    if missing_widths:
        warnings.append('部分道路缺少宽度记录，使用本次配置的假设宽度；不是现场测量值。')
    if simulation_widths:
        warnings.append(f'{simulation_widths}条道路采用 SUMO 仿真宽度；其中的模型参数与实测宽度需区分。')
    grid = settings['grid_m']
    x0, y0, x1, y1 = walkable.bounds
    if math.ceil((x1 - x0) / grid) * math.ceil((y1 - y0) / grid) > 40000:
        raise ValueError('区域跨度过大，请缩小边界或增大计算网格')
    cells, keys, lookup = [], [], {}
    for iy in range(math.ceil((y1 - y0) / grid)):
        for ix in range(math.ceil((x1 - x0) / grid)):
            cell = box(x0 + ix * grid, y0 + iy * grid, x0 + (ix + 1) * grid, y0 + (iy + 1) * grid).intersection(walkable)
            # Preserve EVERY connected piece. Keeping only the largest piece
            # silently cuts curved paths and two lanes passing through one grid.
            for part in pieces(cell, ('Polygon',)):
                if part.area <= 1e-5:
                    continue
                lookup.setdefault((ix, iy), []).append(len(cells))
                keys.append((ix, iy)); cells.append(part)
    if len(cells) > 1800:
        raise ValueError('计算网格超过 1800 个，请增大网格或缩小任务范围')
    adjacency = {i: [] for i in range(len(cells))}
    centers = [np.array(cell.representative_point().coords[0]) for cell in cells]
    for i, (ix, iy) in enumerate(keys):
        for dx, dy in ((1, 0), (0, 1)):
            for j in lookup.get((ix + dx, iy + dy), []):
                # Positive shared edge, not proximity: never jump a real gap.
                if cells[i].intersection(cells[j]).length <= 1e-7:
                    continue
                distance = float(np.linalg.norm(centers[j] - centers[i]))
                if distance <= 1e-7:
                    continue
                adjacency[i].append((j, distance)); adjacency[j].append((i, distance))
    components, remaining = [], set(adjacency)
    while remaining:
        start = next(iter(remaining)); queue = deque([start]); connected = {start}; remaining.remove(start)
        while queue:
            for other, _ in adjacency[queue.popleft()]:
                if other in remaining:
                    remaining.remove(other); connected.add(other); queue.append(other)
        components.append(connected)
    if not components:
        raise ValueError('任务范围没有可计算的步行空间')
    connected = max(components, key=lambda component: sum(cells[i].area for i in component))
    if len(connected) < 4:
        raise ValueError('步行路网未连通，请补充连接道路或调小网格')
    if len(components) > 1:
        warnings.append(f'边界内含 {len(components)} 个不连通路网，本次计算面积最大的连通部分。')
    old_ids = sorted(connected); remap = {old: new for new, old in enumerate(old_ids)}
    cells = [cells[i] for i in old_ids]; centers = np.array([centers[i] for i in old_ids])
    adjacency = {remap[i]: [(remap[j], distance) for j, distance in adjacency[i] if j in remap] for i in old_ids}
    used = set(); gates = []; distances = []
    configured = settings.get('exit_points') or {}
    gate_ids = sorted(configured) if configured else ['A', 'B', 'C']
    gate_labels = settings.get('exit_labels') or {}
    if configured:
        for label in gate_ids:
            point = np.array(forward(*configured[label]))
            # Snap to an actual walkable cell, never create a shortcut through obstacles.
            nearest = min(range(len(cells)), key=lambda i: cells[i].distance(Point(point)))
            distance = float(cells[nearest].distance(Point(point)))
            if distance > max(30, grid * 2):
                raise ValueError(f'{label} 出口距离可计算路网 {distance:.0f} 米，请在地图上重新选取该出口或补充连接道路')
            if nearest in used:
                raise ValueError('两个出口落入同一计算网格，请调整出口位置或减小网格')
            used.add(nearest); gates.append(nearest); distances.append(distance)
        warnings.append('模拟通道使用任务保存的坐标，并吸附到可行走网格；编号不代表官方出口，实际开放状态和宽度仍需核对。')
        if settings.get('exit_points_note'):
            warnings.append(settings['exit_points_note'])
    else:
        for axis, sign in ((0, 1), (1, 1), (1, -1)):
            ordered = np.argsort(centers[:, axis] * sign)
            gate = next(int(i) for i in ordered if int(i) not in used)
            used.add(gate); gates.append(gate); distances.append(0.)
        warnings.append('A/B/C 出口尚未配置，按路网西侧、南侧、北侧推定，须在任务预测配置中设置实际出口。')
    if len(used) >= len(cells):
        raise ValueError('通道已占满全部计算网格，请减少通道或减小网格')
    attraction = settings.get('attraction_point')
    target = np.array(forward(*attraction) if attraction else polygon.representative_point().coords[0])
    if attraction and min(cell.distance(Point(target)) for cell in cells) > max(30, grid * 2):
        raise ValueError('聚集点距离可行走路网过远，请在地图上重新设置。')
    core = next(int(i) for i in np.argsort(np.linalg.norm(centers - target, axis=1)) if int(i) not in used)
    def shortest(start, end):
        queue = [(0., start)]; cost = {start: 0.}; previous = {}
        while queue:
            distance, node = heapq.heappop(queue)
            if node == end:
                result = [end]
                while result[-1] != start:
                    result.append(previous[result[-1]])
                return result[::-1]
            if distance > cost[node]:
                continue
            for other, length in adjacency[node]:
                value = distance + length
                if value < cost.get(other, float('inf')):
                    cost[other] = value; previous[other] = node; heapq.heappush(queue, (value, other))
        raise ValueError('入口与聚集点之间没有连通路径')
    routes = [shortest(gate, core) for gate in gates]
    public_cells = [{'id': f'cell-{i}', 'geometry': mapping(transform(backward, cell)),
                     'center': list(backward(*centers[i])), 'area_m2': round(cell.area, 3)} for i, cell in enumerate(cells)]
    warnings.append(('聚集点使用已配置坐标并吸附到可行走网格；' if attraction else '聚集点按区域内部点自动推定；') + '展示路径是聚集点至出口的路网最短路径，不代表现场实际人群轨迹。')
    return {'areas': np.array([cell.area for cell in cells]), 'centers': centers,
            'cell_shapes': cells, 'walkable_geometry': unary_union(cells), 'to_wgs84': backward,
            'routes': routes, 'gates': gates, 'gate_ids': gate_ids, 'core': core, 'adjacency': adjacency,
            'public': {'boundary': task['region']['geometry'], 'cells': public_cells,
                'routes': [{'id': f'route-{gate_ids[i].lower()}', 'name': f'至 {gate_labels.get(gate_ids[i], gate_ids[i] + " 模拟通道")}', 'cell_ids': route[::-1], 'direction': 'core_to_exit',
                            'coordinates': [list(backward(*centers[j])) for j in route[::-1]],
                            'length_m': round(sum(np.linalg.norm(centers[b]-centers[a]) for a,b in zip(route, route[1:])), 2)} for i, route in enumerate(routes)],
                'gates': [{'id': gate_ids[i], 'name': gate_labels.get(gate_ids[i], f'{gate_ids[i]} 模拟通道'), 'cell_id': gate, 'center': list(backward(*centers[gate])),
                           'position_mode': 'configured' if configured else 'inferred', 'configured_position': configured.get(gate_ids[i]),
                           'snap_distance_m': round(distances[i], 2)} for i,gate in enumerate(gates)],
                'core_cell': core, 'core_position_mode': 'configured' if attraction else 'inferred', 'configured_attraction': attraction,
                'walkable_area_m2': round(sum(cell.area for cell in cells), 2),
                'source_walkable_area_m2': round(walkable.area, 2), 'component_count': len(components),
                'sumo_join_tolerance_m': .01 if roads.get('simulation_network') and not settings['allow_boundary_walkable'] else 0,
                'discarded_walkable_area_m2': round(walkable.area - sum(cell.area for cell in cells), 2),
                'boundary_area_m2': round(polygon.area, 2), 'grid_m': grid, 'vertical_scope': 'surface_only',
                'geometry_version': 'pedestrian-assets-3-preserve-fragments', 'excluded_other_level_features': lower_levels, 'warnings': warnings}}
