"""Synthetic, local 60 × 40 m plaza; never presented as a measured site."""
from pyproj import CRS, Transformer
from shapely.geometry import box, mapping
from shapely.ops import transform

from .inputs import normalize_settings


def monument_inputs():
    """Use the existing SUMO-exported site; demand is an explicit small trial."""
    import json
    from pathlib import Path
    from shapely.geometry import shape
    root = Path(__file__).resolve().parents[2]
    roads = json.loads((root / 'data_service/resources/geography/shanghai-monument-sumo.geojson').read_text())
    config = json.loads((root / 'config/crowd_hotspots.json').read_text())
    hotspot = next(item for item in config['hotspots'] if item['id'] == 'people_heroes_monument')
    positions = []
    for edge in hotspot['park_entry_edges']:
        feature = next(item for item in roads['features'] if item['properties'].get('sumo_edge_id') == edge)
        positions.append(list(shape(feature['geometry']).interpolate(.5, normalized=True).coords[0]))
    roads['warnings'] = [*roads['quality']['warnings'], '试运行采用现有纪念塔路网；人数、来流、驻留和时间为小规模测试假设，不是实测客流。']
    task = {'id': 'monument-three-model-demo', 'revision': 1, 'title': '上海人民英雄纪念塔 · 三模型试运行', 'event_id': 'monument-demo',
            'region': {'mode': 'environment', 'environment_id': 'shanghai-monument-portrait-region', 'name': '上海人民英雄纪念塔 / 黄浦公园', 'geometry': roads['boundary']},
            'time_window': '2026-10-21 18:00 至 2026-10-21 18:05', 'components': {}}
    settings = normalize_settings(task, {'initial_crowd': 80, 'arrival_rate_per_minute': 8, 'dwell_minutes': 1,
        'grid_m': 4, 'sample_minutes': 1, 'path_width_m': 2,
        'attraction_point': [hotspot['anchor']['longitude'], hotspot['anchor']['latitude']],
        'exit_points': dict(zip('AB', positions)), 'exit_labels': {'A': '西北侧步道连接', 'B': '南侧步道连接'},
        'exit_points_note': '位置取既有热点配置 park_entry_edges 对应步道的中点，吸附到网格；不是实测园门或官方出口编号。',
        'entrance_shares': [.6, .4], 'arrival_interpolation': 'step',
        'arrival_schedule': [{'minute': 0, 'rate': 8}, {'minute': 2, 'rate': 0}]})
    return task, roads, settings


def demo_inputs():
    metric = CRS.from_proj4('+proj=aeqd +lat_0=39.1 +lon_0=117.2 +datum=WGS84 +units=m')
    to_geo = Transformer.from_crs(metric, 'EPSG:4326', always_xy=True).transform
    boundary = mapping(transform(to_geo, box(-30, -20, 30, 20)))
    obstacle = mapping(transform(to_geo, box(-4, -12, 4, 4)))
    task = {'id': 'synthetic-three-model-demo', 'revision': 1, 'title': '三模型试运行 · 虚构广场', 'event_id': 'synthetic-demo',
            'region': {'name': '60 × 40 米示例广场（非真实场地）', 'geometry': boundary},
            'time_window': '2026-10-21 18:00 至 2026-10-21 18:03', 'components': {}}
    feature = lambda geometry: {'type': 'Feature', 'geometry': geometry, 'properties': {}}
    roads = {'features': [], 'walkable_areas': {'type': 'FeatureCollection', 'features': [feature(boundary)]},
             'obstacles': {'type': 'FeatureCollection', 'features': [feature(obstacle)]},
             'warnings': ['这是虚构场地和假设来流，仅用于验证三模型流程，不是现场预测。']}
    settings = normalize_settings(task, {'initial_crowd': 60, 'arrival_rate_per_minute': 20, 'dwell_minutes': 1,
        'grid_m': 8, 'sample_minutes': 1, 'emergency_open_minute': 1, 'path_width_m': 2,
        'exit_points': {'A': list(to_geo(-26, 12)), 'B': list(to_geo(26, -12)), 'C': list(to_geo(22, 16))},
        'exit_labels': {'A': '西侧通道', 'B': '东侧通道', 'C': '北侧通道'},
        'entrance_shares': [.7, .3, 0], 'arrival_interpolation': 'step',
        'arrival_schedule': [{'minute': 0, 'rate': 20}, {'minute': 2, 'rate': 0}]})
    return task, roads, settings
