"""Official AMap current-road conditions. Never infer pedestrian counts from speed.

The JS traffic layer and the advanced Web traffic API have separate credentials
and entitlements. Web credentials stay server-side; unavailable data stay null.
"""
import asyncio
import copy
import hashlib
import logging
import math
import os
import time
from datetime import datetime, timezone

import httpx

from ..config import load_database_env
from ..schemas.environments import geometry_bounds

DOCS = 'https://lbs.amap.com/api/webservice/guide/api-advanced/traffic-situation-inquiry'
URL = 'https://restapi.amap.com/v3/traffic/status/circle'
LABELS = {0: '未知', 1: '畅通', 2: '缓行', 3: '拥堵', 4: '严重拥堵'}


class _TrafficRequestLogFilter(logging.Filter):
    def filter(self, record):
        # httpx INFO logs include query strings; AMap puts credentials there.
        return URL not in record.getMessage()


logging.getLogger('httpx').addFilter(_TrafficRequestLogFilter())


def gcj02(lon, lat):
    """Same WGS84->GCJ02 display transform used by the frontend map."""
    if not 72.004 <= lon <= 137.8347 or not .8293 <= lat <= 55.8271:
        return [lon, lat]
    x, y = lon - 105, lat - 35
    common = (20 * math.sin(6*x*math.pi) + 20*math.sin(2*x*math.pi))*2/3
    dlat = -100 + 2*x + 3*y + .2*y*y + .1*x*y + .2*math.sqrt(abs(x)) + common
    dlat += (20*math.sin(y*math.pi) + 40*math.sin(y/3*math.pi))*2/3
    dlat += (160*math.sin(y/12*math.pi) + 320*math.sin(y*math.pi/30))*2/3
    dlon = 300 + x + 2*y + .1*x*x + .1*x*y + .1*math.sqrt(abs(x)) + common
    dlon += (20*math.sin(x*math.pi) + 40*math.sin(x/3*math.pi))*2/3
    dlon += (150*math.sin(x/12*math.pi) + 300*math.sin(x/30*math.pi))*2/3
    rad = lat/180*math.pi
    magic = 1 - .006693421622965943*math.sin(rad)**2
    return [lon + dlon*180/(6378245/math.sqrt(magic)*math.cos(rad)*math.pi),
            lat + dlat*180/((6378245*(1-.006693421622965943))/(magic*math.sqrt(magic))*math.pi)]


def number(value, upper):
    try:
        result = float(str(value).rstrip('%'))
        return result if math.isfinite(result) and 0 <= result <= upper else None
    except (ValueError, TypeError):
        return None


def normalize(value):
    data = value.get('trafficinfo')
    if not isinstance(data, dict):
        raise ValueError('高德未返回有效路况资料')
    evaluation = data.get('evaluation') or {}
    if not isinstance(evaluation, dict):
        evaluation = {}
    roads = []
    for row in (data.get('roads') or [])[:1000]:
        if not isinstance(row, dict):
            continue
        status = number(row.get('status'), 4)
        status = int(status) if status is not None and status.is_integer() else 0
        coords = []
        try:
            for pair in str(row.get('polyline', '')).split(';')[:10000]:
                lon, lat = (float(v) for v in pair.split(','))
                if not math.isfinite(lon+lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
                    raise ValueError('坐标无效')
                coords.append([lon, lat])
        except (ValueError, TypeError):
            coords = []
        roads.append({'name': str(row.get('name') or '未命名道路')[:200], 'status': status, 'status_label': LABELS[status],
                      'speed_kmh': number(row.get('speed'), 300), 'direction': str(row.get('direction') or '')[:300],
                      'coordinates': coords if len(coords) > 1 else [], 'coordinate_system': 'GCJ02'})
    return {'description': str(data.get('description') or '')[:4000],
            'evaluation': {k: number(evaluation.get(k), 100) for k in ['expedite', 'congested', 'blocked', 'unknown']},
            'roads': sorted(roads, key=lambda r: (-r['status'], r['name'])),
            'vehicle_count': None, 'pedestrian_count': None}


class AmapTrafficConnector:
    def __init__(self):
        load_database_env()
        self.cache = {}
        self.lock = asyncio.Lock()

    async def read(self, task, radius=1000):
        if radius not in (500, 1000, 2000):
            raise ValueError('请选择 500、1000 或 2000 米的周边路况范围')
        w, s, e, n = geometry_bounds(task['region']['geometry'])
        center = gcj02((w+e)/2, (s+n)/2)
        base = {'provider': 'amap', 'source_url': DOCS, 'measurement': 'road_conditions',
                'scope': {'center': center, 'coordinate_system': 'GCJ02', 'radius_m': radius},
                'task_time_window': task.get('time_window', ''), 'use': 'current_accessibility_context',
                'fetched_at': None, 'source_observed_at': None, 'vehicle_count': None, 'pedestrian_count': None,
                'roads': [], 'evaluation': {}, 'description': ''}
        key = os.environ.get('CROWDSIM_AMAP_WEB_KEY', '').strip()
        if not key:
            return {**base, 'status': 'unconfigured', 'message': '实时路况图层可直接查看；路段速度与拥堵比例需配置已开通交通态势服务的高德 Web Key。'}
        private_key = os.environ.get('CROWDSIM_AMAP_WEB_PRIVATE_KEY', '').strip()
        cache_key = hashlib.sha256(f'{key}:{private_key}:{center}:{radius}'.encode()).hexdigest()
        async with self.lock:
            now = time.monotonic()
            cached = self.cache.get(cache_key)
            if cached and cached[0] > now:
                return {**copy.deepcopy(cached[1]), 'task_time_window': base['task_time_window']}
            params = {'key': key, 'location': ','.join(f'{v:.6f}' for v in center), 'radius': radius, 'level': 5, 'extensions': 'all', 'output': 'JSON'}
            if private_key:
                params['sig'] = hashlib.md5(('&'.join(f'{k}={params[k]}' for k in sorted(params)) + private_key).encode()).hexdigest()
            try:
                async with httpx.AsyncClient(timeout=10, trust_env=True) as client:
                    response = await client.get(URL, params=params)
                    response.raise_for_status()
                    value = response.json()
                if not isinstance(value, dict) or str(value.get('status')) != '1':
                    code = str(value.get('infocode', 'unknown')) if isinstance(value, dict) else 'unknown'
                    code = code if code.isascii() and code.isdigit() and len(code) <= 6 else 'unknown'
                    # Do not forward raw upstream errors/URLs that could echo credentials.
                    result = {**base, 'status': 'unavailable', 'code': code[:20],
                              'message': '高德暂未提供路段数值，请核对交通态势服务权限、配额和 Web Key 配置。'}
                else:
                    result = {**base, **normalize(value), 'status': 'ready', 'fetched_at': datetime.now(timezone.utc).isoformat(),
                              'message': '当前道路状态、平均车速；获取时间不等同于上游采样时刻。'}
            except (httpx.HTTPError, ValueError, TypeError, ImportError):
                result = {**base, 'status': 'unavailable', 'message': '高德路段数值暂时未返回，请稍后刷新；可继续查看实时地图。'}
            self.cache[cache_key] = (now + (120 if result['status'] == 'ready' else 30), copy.deepcopy(result))
            if len(self.cache) > 100:
                self.cache = {k: v for k, v in self.cache.items() if v[0] > now}
            return result
