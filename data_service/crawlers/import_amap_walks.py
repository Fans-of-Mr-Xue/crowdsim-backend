"""Normalize explicitly queried AMap walking routes into a versioned WGS84 asset.

This is a route sample, not an export of AMap's full pedestrian database.
Keep the raw, credential-free SDK response for provenance. Only consecutive
steps of the SAME returned route may close sub-metre rounding seams. Never
connect unrelated routes or carry SUMO connections onto different geometry.
"""
import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from shapely.geometry import LineString
from shapely.ops import linemerge, unary_union

from ..connectors.amap_traffic import gcj02
from ..services.pedestrian_network import clip_network, collection, pieces

DOCS = 'https://lbs.amap.com/api/javascript-api-v2/guide/services/navigation'


def wgs84(point):
    lon, lat = point
    x, y = lon, lat
    for _ in range(5):
        a, b = gcj02(x, y)
        x -= a - lon
        y -= b - lat
    return [round(x, 9), round(y, 9)]


def build(rows, boundary, supplements=None):
    lines = []
    used = []
    seams = []
    for row in rows:
        if row.get('status') != 'complete' or row.get('include_in_network') is False:
            continue
        routes = row.get('result', {}).get('routes') or []
        if not routes:
            continue
        used.append(row['id'])
        chain = []
        for step in routes[0].get('steps', []):
            path = step.get('path', [])
            if not path:
                continue
            if chain:
                a, b = chain[-1], path[0]
                distance = math.hypot((a[0]-b[0])*111320*math.cos(math.radians(a[1])), (a[1]-b[1])*111320)
                if distance > 1:
                    if len(set(map(tuple, chain))) > 1:
                        lines.append(LineString(chain))
                    chain = []
                elif distance > .001:
                    seams.append(round(distance, 3))
            chain.extend(path)
        if len(set(map(tuple, chain))) > 1:
            lines.append(LineString(chain))
    if not lines:
        raise ValueError('没有成功返回的步行路线，不能生成路网')
    # Route responses are already GCJ02. Node shared segments there before
    # converting; use exactly one inverse transform for the canonical asset.
    merged = unary_union(lines)
    if merged.geom_type == 'MultiLineString':
        merged = linemerge(merged)
    features = []
    for index, line in enumerate(pieces(merged, {'LineString'})):
        features.append({'type': 'Feature', 'geometry': {'type': 'LineString', 'coordinates': [wgs84(p) for p in line.coords]},
            'properties': {'highway': 'footway', 'foot': 'yes', 'name': f'高德步行通道 {index + 1:02d}',
                'source_type': 'amap_walking', 'source_url': DOCS, 'geometry_source': 'amap_walking_route',
                'width_source': 'missing', 'level_source': 'unmapped_assumed_surface', 'display_level': '0'}})
    if supplements:
        from shapely.geometry import shape, mapping, MultiPoint
        from shapely.ops import snap
        # Split known junctions without materially moving official geometry.
        anchors = MultiPoint([p for f in supplements['features'] for p in f['geometry']['coordinates']])
        for feature in features:
            feature['geometry'] = json.loads(json.dumps(mapping(snap(shape(feature['geometry']), anchors, 1e-8))))
        features.extend(supplements['features'])
    network = collection(features)
    network['source'] = {'provider': 'amap_walking', 'label': '高德步行路线核对版', 'coordinateSystem': 'WGS84',
        'originalCoordinateSystem': 'GCJ02', 'scope': 'boundary_clipped', 'url': DOCS,
        'retrievedAt': datetime.now(timezone.utc).isoformat(), 'query_count': len(rows), 'successful_query_count': sum(r.get('status') == 'complete' for r in rows), 'used_route_count': len(used),
        'coverage_note': '按既有通道及南北接入查询的路线并集；不是高德全部道路导出，也不是现场测绘。',
        'width_status': 'missing_use_task_assumption',
        'consecutive_step_seams': {'count': len(seams), 'maximum_m': max(seams, default=0), 'policy': '仅连接同一高德路线连续步骤中不超过1米的坐标舍入缝隙'}}
    network['warnings'] = ['高德步行路线仅覆盖本次查询到的通道；未查询到的支路、广场与层级仍待核对。',
        '路宽和开放条件没有实测数据；计算采用任务共用宽度假设。']
    network['temporal'] = {'observed_at': network['source']['retrievedAt'], 'historical_verified': False,
        'note': '当前地图路线；不表示研究日期的开放状态。'}
    network['evidence'] = [{'title': '高德步行路线原始响应', 'url': DOCS,
        'asset': 'geography/shanghai-monument-amap-walks.json', 'query_ids': used}]
    if supplements:
        network['source']['label'] = '高德步行路线与底图补绘'
        network['source']['supplement_count'] = len(supplements['features'])
        network['source']['coverage_note'] = '已按底图核对园内支路与滨江步道；区分接口路线与人工补绘，不代表实测完整路网。'
        network['warnings'].append('底图补绘路段按可通行假设参与计算；尚未核实现场开放状态、层级与路宽。')
        network['evidence'].append({'title': '底图补绘记录', 'asset': 'geography/shanghai-monument-amap-supplements.geojson', 'method': '在高德地图逐点拾取并记录来源，接点与已查询通道核对'})
    network['builder_version'] = 'amap-walking-routes-2' 
    return clip_network(network, boundary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--responses', required=True)
    parser.add_argument('--boundary-asset', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--supplements')
    args = parser.parse_args()
    source = json.loads(Path(args.boundary_asset).read_text())
    boundary = source.get('boundary') or source
    result = build(json.loads(Path(args.responses).read_text()), boundary, json.loads(Path(args.supplements).read_text()) if args.supplements else None)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({'features': len(result['features']), 'components': result['topology']['component_sizes']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
