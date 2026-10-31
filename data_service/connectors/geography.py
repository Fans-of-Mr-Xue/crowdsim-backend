"""Configurable Photon place lookup and OSM Overpass geometry/road retrieval."""
import asyncio
import hashlib
import json
import logging
import math
import os
import time
from urllib.parse import urlsplit
from datetime import datetime, timedelta, timezone

import httpx
from ..config import load_database_env
from ..schemas.environments import area_km2, geometry_bounds, validate_geometry, validate_roads

logger = logging.getLogger(__name__)


class GeographyError(Exception):
    pass


def join_rings(segments):
    pending = [list(segment) for segment in segments if len(segment) >= 2]
    rings = []
    while pending:
        ring = pending.pop()
        while ring[0] != ring[-1]:
            found = False
            for index, segment in enumerate(pending):
                if ring[-1] == segment[0]:
                    ring.extend(segment[1:])
                elif ring[-1] == segment[-1]:
                    ring.extend(list(reversed(segment))[1:])
                elif ring[0] == segment[-1]:
                    ring = segment[:-1] + ring
                elif ring[0] == segment[0]:
                    ring = list(reversed(segment))[:-1] + ring
                else:
                    continue
                pending.pop(index)
                found = True
                break
            if not found:
                raise GeographyError('地理库的边界环不完整，请改选附近面状区域或手动绘制')
        rings.append(ring)
    return rings


def inside(point, ring):
    x, y = point
    hit = False
    for a, b in zip(ring, ring[1:]):
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            hit = not hit
    return hit


def osm_geometry(element):
    def points(items):
        return [[p['lon'], p['lat']] for p in items]
    if element['type'] == 'way':
        ring = points(element.get('geometry', []))
        if len(ring) >= 4 and ring[0] == ring[-1]:
            return validate_geometry({'type': 'Polygon', 'coordinates': [ring]})
        return None
    if element['type'] != 'relation':
        return None
    outer, inner = [], []
    for member in element.get('members', []):
        if member.get('type') in ('way', 'relation') and member.get('role', '') in ('', 'outer', 'inner') and (member.get('type') == 'relation' or not member.get('geometry')):
            raise GeographyError('该对象边界未完整返回，请改选面状区域或导入完整 GeoJSON')
        if member.get('type') == 'way' and member.get('geometry') and member.get('role', '') in ('', 'outer', 'inner'):
            (inner if member.get('role') == 'inner' else outer).append(points(member['geometry']))
    if not outer:
        return None
    polygons = [[ring] for ring in join_rings(outer)]
    for hole in join_rings(inner):
        parent = next((polygon for polygon in polygons if inside(hole[0], polygon[0])), None)
        if parent is None:
            raise GeographyError('地理库边界包含无法归属的内环，请手动确认边界')
        parent.append(hole)
    return validate_geometry({'type': 'Polygon', 'coordinates': polygons[0]} if len(polygons) == 1 else {'type': 'MultiPolygon', 'coordinates': polygons})


def expand_osm_elements(value):
    """Join the official read API's node references into complete way geometries."""
    elements = value.get('elements', [])
    nodes = {e['id']: e for e in elements if e.get('type') == 'node'}
    ways = {e['id']: e for e in elements if e.get('type') == 'way'}
    for way in ways.values():
        refs = way.get('nodes', [])
        if refs and all(ref in nodes for ref in refs):
            way['geometry'] = [{'lon': nodes[ref]['lon'], 'lat': nodes[ref]['lat']} for ref in refs]
    for relation in (e for e in elements if e.get('type') == 'relation'):
        for member in relation.get('members', []):
            if member.get('type') == 'way' and member.get('ref') in ways:
                member['geometry'] = ways[member['ref']].get('geometry', [])
    return value


class GeographyConnector:
    def __init__(self, database):
        load_database_env()
        self.photon_url = os.environ.get('CROWDSIM_PHOTON_URL', 'https://photon.komoot.io').rstrip('/')
        self.nominatim_url = os.environ.get('CROWDSIM_NOMINATIM_URL', 'https://nominatim.openstreetmap.org').rstrip('/')
        self.osm_url = os.environ.get('CROWDSIM_OSM_API_URL', 'https://api.openstreetmap.org/api/0.6').rstrip('/')
        if self.osm_url == 'https://www.openstreetmap.org/api/0.6':
            self.osm_url = 'https://api.openstreetmap.org/api/0.6'
        self.overpass_urls = [url.strip() for url in os.environ.get('CROWDSIM_OVERPASS_URLS', 'https://overpass-api.de/api/interpreter,https://overpass.kumi.systems/api/interpreter').split(',') if url.strip()]
        self.cache = database['geography_cache']
        self.lock = asyncio.Lock()
        self.last_request = 0
        self.headers = {'User-Agent': 'CrowdSim-EnvironmentService/1.0 (region profiles)', 'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.7'}

    async def initialize(self):
        await asyncio.to_thread(self.cache.create_index, 'expires_at', expireAfterSeconds=0)

    async def request(self, key, url, *, params=None, data=None, timeout=30):
        cache_key = hashlib.sha256(key.encode()).hexdigest()
        cached = await asyncio.to_thread(self.cache.find_one, {'_id': cache_key, 'expires_at': {'$gt': datetime.now(timezone.utc)}})
        if cached:
            return cached['value']
        async with self.lock:
            cached = await asyncio.to_thread(self.cache.find_one, {'_id': cache_key, 'expires_at': {'$gt': datetime.now(timezone.utc)}})
            if cached:
                return cached['value']
            await asyncio.sleep(max(0, 1.1 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
        # Hold the rate-limit lock only until a request starts. A slow upstream
        # must not block all other searches, boundary reads and road requests.
        async def fetch():
            # HTTPX also honors ALL_PROXY/SOCKS, unlike aiohttp's trust_env.
            async with httpx.AsyncClient(headers=self.headers, trust_env=True, follow_redirects=True,
                                         timeout=httpx.Timeout(timeout, connect=min(5, timeout))) as client:
                async with client.stream('POST' if data is not None else 'GET', url, params=params, data=data) as response:
                    if response.status_code != 200:
                        logger.warning('Geography upstream %s returned HTTP %s', urlsplit(url).hostname, response.status_code)
                        raise GeographyError(f'在线地理来源返回 HTTP {response.status_code}，可重试或选择环境库 / 自定义范围')
                    raw = bytearray()
                    async for chunk in response.aiter_bytes(128 * 1024):
                        raw.extend(chunk)
                        if len(raw) > 12 * 1024 * 1024:
                            raise GeographyError('地理数据过大，请缩小区域后获取')
                    return json.loads(raw)
        try:
            value = await asyncio.wait_for(fetch(), timeout=timeout)
        except ImportError:
            raise GeographyError('地理服务缺少代理依赖，请在后端安装更新后的 requirements.txt') from None
        except (httpx.HTTPError, asyncio.TimeoutError, json.JSONDecodeError):
            logger.warning('Geography upstream %s was unavailable or timed out', urlsplit(url).hostname)
            raise GeographyError('在线地理读取超时或网络不可用，可以重试或更换区域来源') from None
        if not isinstance(value, dict) or value.get('remark') or value.get('error'):
            raise GeographyError('地理数据查询未完整完成，请缩小范围或稍后重试')
        await asyncio.to_thread(self.cache.replace_one, {'_id': cache_key}, {'_id': cache_key, 'value': value, 'expires_at': datetime.now(timezone.utc) + timedelta(days=7)}, upsert=True)
        return value

    async def search(self, query, *, city_code=None, scope='place'):
        if city_code:
            from .region_catalog import find_city
            from .scoped_place_search import ScopedPlaceSearch
            city = find_city(city_code)
            query = query.strip()
            if scope not in ('city', 'place') or (scope == 'place' and not 2 <= len(query) <= 200):
                raise ValueError('请输入 2–200 个字符的具体地点，或选择预览城市范围')
            try:
                return await asyncio.wait_for(ScopedPlaceSearch(self).search(query, city, city_only=scope == 'city'), timeout=20)
            except asyncio.TimeoutError:
                raise GeographyError('城市范围内的地理搜索超时，可以重试或更换来源') from None
        try:
            return await asyncio.wait_for(self._search(query), timeout=12)
        except asyncio.TimeoutError:
            raise GeographyError('在线区域搜索超时，可以继续选择环境库或自定义范围') from None

    async def _search(self, query, *, center=None, limit=8, timeout=8):
        query = query.strip()
        if not 2 <= len(query) <= 200:
            raise ValueError('请输入 2–200 个字符的地区或地点名称')
        params = {'q': query, 'limit': limit}
        if center:
            params.update(lon=center[0], lat=center[1])
        value = await self.request('photon:' + self.photon_url + ':' + json.dumps(params, sort_keys=True), self.photon_url + '/api/', params=params, timeout=timeout)
        items = []
        for feature in value.get('features', []):
            properties = feature.get('properties', {})
            osm_type = {'N': 'node', 'W': 'way', 'R': 'relation'}.get(properties.get('osm_type'))
            if not osm_type or not properties.get('osm_id'):
                continue
            lon, lat = feature['geometry']['coordinates'][:2]
            items.append({'id': f"{osm_type}/{properties['osm_id']}", 'osm_type': osm_type, 'osm_id': properties['osm_id'],
                          'name': properties.get('name') or query, 'locationLabel': ' · '.join(dict.fromkeys(str(properties[k]) for k in ('city', 'district', 'state', 'country') if properties.get(k))),
                          'coordinates': [lat, lon], 'placeType': properties.get('osm_value', ''), 'extent': properties.get('extent'),
                          'sourceUrl': f"https://www.openstreetmap.org/{osm_type}/{properties['osm_id']}"})
        return items

    async def overpass(self, query, *, timeout=30):
        for url in self.overpass_urls:
            try:
                result = await self.request('overpass:' + url + ':' + query, url, data={'data': query}, timeout=timeout)
                if result.get('remark'):
                    raise GeographyError('地理数据查询未完整完成，请缩小范围或稍后重试')
                return result
            except GeographyError:
                if url == self.overpass_urls[-1]:
                    raise
        raise GeographyError('未配置 Overpass 地理数据服务')

    async def boundary(self, osm_type, identifier):
        try:
            return await asyncio.wait_for(self._boundary(osm_type, identifier), timeout=20)
        except asyncio.TimeoutError:
            raise GeographyError('在线边界读取超过 20 秒，当前范围未改变；可重试或更换来源') from None

    async def _boundary(self, osm_type, identifier):
        if osm_type not in ('node', 'way', 'relation') or not str(identifier).isdigit() or not 0 < int(identifier) < 100000000000:
            raise ValueError('请选择有效的地理检索结果')
        # Exact object reads avoid a large spatial query and support small parks well.
        url = f'{self.osm_url}/{osm_type}/{int(identifier)}' + ('/full.json' if osm_type != 'node' else '.json')
        try:
            value = expand_osm_elements(await self.request(url, url, timeout=8))
        except GeographyError:
            value = await self.overpass(f'[out:json][timeout:6];{osm_type}({int(identifier)});out geom;', timeout=6)
        element = next((e for e in value.get('elements', []) if e.get('type') == osm_type and e.get('id') == int(identifier)), None)
        if element is None:
            raise GeographyError('地理库中未找到该对象，请重新检索')
        geometry = osm_geometry(element)
        tags = element.get('tags', {})
        source = {'provider': 'OpenStreetMap', 'label': 'OpenStreetMap 地理对象边界', 'osm_type': osm_type, 'osm_id': int(identifier), 'url': f'https://www.openstreetmap.org/{osm_type}/{identifier}', 'coordinateSystem': 'WGS84', 'retrievedAt': datetime.now(timezone.utc).isoformat()}
        if geometry:
            bbox = geometry_bounds(geometry)
            return {'geometry': geometry, 'coordinates': [(bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2], 'name': tags.get('name:zh') or tags.get('name', ''), 'boundarySource': source, 'areaKm2': area_km2(geometry), 'message': ''}
        return {'geometry': None, 'boundarySource': source, 'name': tags.get('name:zh') or tags.get('name', ''), 'message': '该对象仅有地点或线状信息，没有可用的区域边界。可选择附近公园或在地图上绘制研判范围。'}

    async def roads(self, geometry):
        geometry = validate_geometry(geometry)
        west, south, east, north = geometry_bounds(geometry)
        width = abs(east - west) * 111 * max(.05, math.cos(math.radians((south + north) / 2)))
        height = abs(north - south) * 111
        if width * height > 200:
            return {'roads': {'type': 'FeatureCollection', 'features': []}, 'status': 'unavailable', 'message': '当前区域较大，详细路网请在区内、公园或场地范围获取'}
        # Keep terrain/entrance tags with the roads, including on Overpass fallback.
        bounds = f'{south},{west},{north},{east}'
        rich_query = (f'[out:json][timeout:25];('
                      f'way["highway"]({bounds});way["building"]({bounds});way["building:part"]({bounds});'
                      f'way["barrier"]({bounds});way["area:highway"]({bounds});'
                      f'way["landuse"~"^(grass|flowerbed)$"]({bounds});way["natural"="water"]({bounds});'
                      f'node["entrance"]({bounds});node["barrier"]({bounds}););out geom;')
        # The OSM read API only allows small extracts. Larger regions use Overpass.
        if width * height <= 4 and (east - west) * (north - south) <= .01:
            url = self.osm_url + '/map.json'
            bbox = f'{west},{south},{east},{north}'
            try:
                value = expand_osm_elements(await self.request(url + ':' + bbox, url, params={'bbox': bbox}))
            except GeographyError:
                value = await self.overpass(rich_query)
        else:
            value = await self.overpass(rich_query)
        from ..services.pedestrian_network import osm_assets
        source = {'provider': 'OpenStreetMap', 'url': 'https://www.openstreetmap.org/copyright',
                  'coordinateSystem': 'WGS84', 'scope': 'boundary_clipped', 'bbox': [west, south, east, north],
                  'retrievedAt': datetime.now(timezone.utc).isoformat(), 'label': 'OpenStreetMap 步行空间与设施'}
        roads = validate_roads(osm_assets(value, geometry, source))
        return {'roads': roads, 'status': 'available' if roads['features'] else 'unavailable',
                'message': f"已获取 {len(roads['features'])} 条步行通道、{roads['quality']['obstacle_count']} 处障碍，保留高程层与原始标签；宽度缺项待补充"}
