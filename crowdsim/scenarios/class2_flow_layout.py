"""Deterministic class-two layout; identical allocation rules to the map editor."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
MAP_PATH = ROOT / 'config/during_presets/memorial_flow_network_v1.json'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


@lru_cache(maxsize=4)
def _read_map(stamp):
    data = json.loads(MAP_PATH.read_text())
    if digest({key: value for key, value in data.items() if key != 'geometrySha256'}) != data['geometrySha256']:
        raise ValueError('人流地图几何校验失败，请重新导出地图')
    if hashlib.sha256((ROOT / 'scenarios/shanghai_bund/bund.net.xml').read_bytes()).hexdigest() != data['sourceSha256']:
        raise ValueError('人流地图与 SUMO 路网版本不一致，请重新导出地图')
    for lane in data['lanes']:
        lane['_originXY'] = data['originXY']
        if 'distances' in lane and 'length' in lane:
            continue
        ds = [0.0]
        for a, b in zip(lane['path'], lane['path'][1:]):
            ds.append(ds[-1] + math.dist(a, b))
        lane['distances'], lane['length'] = ds, ds[-1]
    return data


def flow_network():
    return _read_map((MAP_PATH.stat().st_mtime_ns, (ROOT / 'scenarios/shanghai_bund/bund.net.xml').stat().st_mtime_ns))


def validate_design(raw, count=None):
    data = flow_network()
    if (not isinstance(raw, dict) or set(raw) - {'version', 'location_id', 'network_sha256', 'flows'}
            or type(raw.get('version')) is not int or raw['version'] != 1 or raw.get('location_id') != 'memorial-tower'
            or raw.get('network_sha256') != data['sourceSha256'] or not isinstance(raw.get('flows'), list)
            or not 1 <= len(raw['flows']) <= 20):
        raise ValueError('请提供完整的纪念塔人流配置（1–20股），旧固定聚集方案不再执行')
    by_id = {lane['id']: lane for lane in data['lanes']}
    ids, flows = set(), []
    for raw_flow in raw['flows']:
        if not isinstance(raw_flow, dict) or set(raw_flow) - {'id', 'name', 'count', 'density', 'speed', 'origin', 'destination', 'origin_hint', 'destination_hint'}:
            raise ValueError('人流字段无效')
        f = {key: deepcopy(raw_flow.get(key)) for key in ['id', 'name', 'count', 'density', 'speed', 'origin', 'destination']}
        if not isinstance(f['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', f['id']) or f['id'] in ids:
            raise ValueError('人流编号无效或重复')
        ids.add(f['id'])
        if not isinstance(f['name'], str) or not 1 <= len(f['name'].strip()) <= 40:
            raise ValueError('请填写人流名称')
        f['name'] = f['name'].strip()
        if type(f['count']) is not int or not 1 <= f['count'] <= 10000:
            raise ValueError('每股人流人数须为 1–10000 的整数')
        for key, maximum in [('density', 20), ('speed', 2.5)]:
            if type(f[key]) not in (int, float) or not math.isfinite(f[key]) or not 0 < f[key] <= maximum:
                raise ValueError(f"{f['name']}：{key} 无效")
        for kind in ['origin', 'destination']:
            anchor = f[kind]
            if (not isinstance(anchor, dict) or set(anchor) != {'lane_id', 'offset'} or anchor.get('lane_id') not in by_id
                    or type(anchor.get('offset')) not in (int, float) or not math.isfinite(anchor['offset'])
                    or not 0 <= anchor['offset'] <= by_id[anchor['lane_id']]['length'] + .001):
                raise ValueError(f"{f['name']}：请在当前步行路网上标记{kind}")
            anchor['offset'] = min(anchor['offset'], by_id[anchor['lane_id']]['length'])
        flows.append(f)
    total = sum(f['count'] for f in flows)
    if total > 10000 or count is not None and (type(count) is not int or total != count):
        raise ValueError('各股人流人数合计必须等于研究总人数，且不超过10000人')
    return {'version': 1, 'location_id': 'memorial-tower', 'network_sha256': data['sourceSha256'], 'flows': flows}


def _node_distances(anchor, lanes, by_id):
    lane = by_id[anchor['lane_id']]
    distances = {lane['from']: anchor['offset']}
    distances[lane['to']] = min(distances.get(lane['to'], math.inf), lane['length'] - anchor['offset'])
    visited = set()
    while True:
        candidates = [(d, node) for node, d in distances.items() if node not in visited]
        if not candidates:
            return distances
        minimum, node = min(candidates)
        visited.add(node)
        for item in lanes:
            other = item['to'] if item['from'] == node else item['from'] if item['to'] == node else None
            if other is not None and minimum + item['length'] < distances.get(other, math.inf):
                distances[other] = minimum + item['length']


def _subtract(ranges, reserved):
    for a, b in reserved:
        next_ranges = []
        for start, end in ranges:
            if b <= start or a >= end:
                next_ranges.append((start, end))
            else:
                next_ranges.extend((x, y) for x, y in [(start, min(a, end)), (max(b, start), end)] if y - x > 1e-7)
        ranges = next_ranges
    return ranges


def position(lane, offset, lateral=0):
    i = next((i for i in range(1, len(lane['distances'])) if lane['distances'][i] >= offset), len(lane['path']) - 1)
    a, b = lane['path'][i-1:i+1]
    length = math.dist(a, b) or 1
    ratio = (offset - lane['distances'][i-1]) / length
    x, y = a[0] + (b[0]-a[0])*ratio - (b[1]-a[1])*lateral/length, a[1] + (b[1]-a[1])*ratio + (b[0]-a[0])*lateral/length
    origin = lane['_originXY']
    original = lane['sourceStart'] + offset * (lane['sourceEnd'] - lane['sourceStart']) / lane['length']
    return {'lane_id': lane['id'], 'offset': offset, 'lateral': lateral, 'source_lane_id': lane['sourceLaneId'],
            'edge': lane['edgeId'], 'position': original / lane['sourceShapeLength'] * lane['sourceLength'],
            'x': x + origin[0], 'y': y + origin[1], 'local_x': x, 'local_y': y}


def footprint(anchor, count, density, reserved):
    lanes = flow_network()['lanes']
    by_id = {lane['id']: lane for lane in lanes}
    nodes, target = _node_distances(anchor, lanes, by_id), count / density

    def ranges(radius):
        parts = []
        for lane in lanes:
            pieces = []
            left, right = radius - nodes.get(lane['from'], math.inf), radius - nodes.get(lane['to'], math.inf)
            if left > 0:
                pieces.append((0, min(lane['length'], left)))
            if right > 0:
                pieces.append((max(0, lane['length']-right), lane['length']))
            if lane['id'] == anchor['lane_id']:
                pieces.append((max(0, anchor['offset']-radius), min(lane['length'], anchor['offset']+radius)))
            merged = []
            for start, end in sorted(pieces):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))
            for start, end in _subtract(merged, reserved.get(lane['id'], [])):
                parts.append({'lane': lane, 'start': start, 'end': end, 'area': (end-start)*lane['width']})
        return parts

    def area(parts):
        return sum(p['area'] for p in parts)
    available = [p for p in ranges(math.inf) if p['lane']['id'] == anchor['lane_id'] or p['lane']['from'] in nodes or p['lane']['to'] in nodes]
    if area(available) + 1e-5 < target:
        raise ValueError('可用步行路段不足以容纳当前人数和密度，请调整标点或参数')
    low, high = 0, sum(lane['length'] for lane in lanes)
    for _ in range(42):
        mid = (low+high)/2
        if area(ranges(mid)) < target:
            low = mid
        else:
            high = mid
    parts = [p for p in ranges(high) if p['area'] > 1e-7]
    amounts = [math.floor(count*p['area']/target) for p in parts]
    remainder = count - sum(amounts)
    for i in sorted(range(len(parts)), key=lambda i: -(count*parts[i]['area']/target - amounts[i]))[:remainder]:
        amounts[i] += 1
    points, strips = [], []
    for part, n in zip(parts, amounts):
        lane, start, end = part['lane'], part['start'], part['end']
        columns = max(1, math.floor(lane['width']*math.sqrt(density) + .5))
        rows = math.ceil(n/columns)
        for j in range(n):
            row, column = divmod(j, columns)
            row_size = min(columns, n-row*columns)
            points.append(position(lane, start + (row+.5)*(end-start)/rows, ((column+.5)/row_size-.5)*lane['width']*.88))
        reserved.setdefault(lane['id'], []).append((start, end))
        strips.append({'lane_id': lane['id'], 'start': start, 'end': end, 'width': lane['width']})
    if len(points) != count:
        raise ValueError('人流布局人数计算不一致')
    return {'area': area(parts), 'points': points, 'ranges': strips}


def build_flow_layout(design):
    origins, destinations, flows, people = {}, {}, [], []
    for f in design['flows']:
        start = footprint(f['origin'], f['count'], f['density'], origins)
        end = footprint(f['destination'], f['count'], f['density'], destinations)
        flows.append({**deepcopy(f), 'origin_layout': start, 'destination_layout': end})
        for i, (source, target) in enumerate(zip(start['points'], end['points'])):
            people.append({'person_id': f"class2.{f['id']}.{i:05d}", 'flow_id': f['id'], 'speed': f['speed'],
                           **source, 'destination': target})
    return {'version': 1, 'geometry_sha256': flow_network()['geometrySha256'], 'flow_design': deepcopy(design),
            'flow_design_sha256': digest(design), 'flows': flows, 'positions': people}
