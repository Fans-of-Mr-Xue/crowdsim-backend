"""Explicit city-scoped searches; OSM IDs still resolve through the boundary API."""
import json
import re
from datetime import datetime, timezone

from ..schemas.environments import area_km2, geometry_bounds, validate_geometry


def short_name(name):
    return str(name or '').removesuffix('市').removesuffix('省')


class ScopedPlaceSearch:
    def __init__(self, connector):
        self.connector = connector

    async def search(self, query, city, *, city_only=False):
        from .geography import GeographyError
        # Nominatim is used only on explicit search/preview clicks, never for
        # autocomplete. Existing unscoped Photon clients keep their behavior.
        params = {'format': 'geojson', 'addressdetails': 1, 'namedetails': 1,
                  'limit': 8, 'accept-language': 'zh-CN,zh,en',
                  'countrycodes': {'810000': 'hk', '820000': 'mo', '710000': 'tw'}.get(city['province_code'], 'cn')}
        if city_only:
            # Municipalities are OSM state boundaries, not the city-center nodes
            # returned by a structured city query.
            if city['province_code'] in ('110000', '120000', '310000', '500000'):
                params.update(state=city['name'], featureType='state')
            else:
                params.update(city=city['name'], featureType='city')
            if city['province'] != city['name']:
                params['state'] = city['province']
            params.update(polygon_geojson=1, polygon_threshold=.00005)
        else:
            params['q'] = ', '.join(dict.fromkeys([query, city['name'], city['province']]))
        url = self.connector.nominatim_url + '/search'
        error = None
        try:
            value = await self.connector.request('nominatim:' + url + ':' + json.dumps(params, sort_keys=True), url, params=params, timeout=7)
            items = self.nominatim_items(value)
            if city_only:
                items = [item for item in items if short_name(item['name']) == short_name(city['name']) and item.get('geometry')]
                items.sort(key=lambda item: item['osm_type'] != 'relation')
            else:
                items = [item for item in items if short_name(city['name']) in item['locationLabel']]
            if items:
                return items
        except GeographyError as exc:
            error = exc
        if not city_only and error is None:
            landmark = re.sub(r'(?:广场|公园|景区|区域)$', '', query).strip()
            if landmark != query and len(landmark) >= 2:
                nearby_params = {**params, 'q': ', '.join(dict.fromkeys([landmark, city['name'], city['province']]))}
                try:
                    value = await self.connector.request('nominatim:' + url + ':' + json.dumps(nearby_params, sort_keys=True), url, params=nearby_params, timeout=6)
                    nearby = [item for item in self.nominatim_items(value) if landmark in item['name'] and short_name(city['name']) in item['locationLabel']]
                    if nearby:
                        # A statue is a useful drawing anchor, not a substitute
                        # polygon for the square the user originally requested.
                        return [{**item, 'matchKind': 'nearby', 'matchedQuery': query} for item in nearby]
                except GeographyError:
                    pass
        # Geographic bias alone is insufficient: also check the returned address
        # so a similarly named landmark in a different city is not silently used.
        items = await self.connector._search(city['name'] if city_only else query,
                                             center=city['center'], limit=15, timeout=7)
        city_name = short_name(city['name'])
        items = [item for item in items if (short_name(item['name']) == city_name and item['osm_type'] in ('relation', 'way') if city_only else
                 city_name in item.get('locationLabel', ''))]
        if not items and error:
            raise error
        if city_only:
            items.sort(key=lambda item: item['osm_type'] != 'relation')
        return items[:8]

    @staticmethod
    def nominatim_items(value):
        items = []
        for feature in value.get('features', []):
            p = feature.get('properties') or {}
            osm_type, osm_id = p.get('osm_type'), p.get('osm_id')
            geometry = feature.get('geometry') or {}
            if osm_type not in ('node', 'way', 'relation') or not osm_id:
                continue
            extra = {}
            if geometry.get('type') in ('Polygon', 'MultiPolygon'):
                geometry = validate_geometry(geometry)
                west, south, east, north = geometry_bounds(geometry)
                coordinates = [(west + east) / 2, (south + north) / 2]
                extra = {'geometry': geometry, 'areaKm2': area_km2(geometry),
                         'boundarySource': {'provider': 'OpenStreetMap', 'label': 'OpenStreetMap 城市行政边界（已简化）',
                            'osm_type': osm_type, 'osm_id': osm_id, 'coordinateSystem': 'WGS84',
                            'url': f'https://www.openstreetmap.org/{osm_type}/{osm_id}',
                            'simplificationDegrees': .00005, 'retrievedAt': datetime.now(timezone.utc).isoformat()}}
            elif geometry.get('type') == 'Point' and isinstance(geometry.get('coordinates'), list) and len(geometry['coordinates']) == 2:
                coordinates = geometry['coordinates']
            else:
                continue
            names = p.get('namedetails') or {}
            name = names.get('name:zh') or names.get('name') or p.get('name') or str(p.get('display_name', '')).split(',')[0]
            items.append({'id': f'{osm_type}/{osm_id}', 'osm_type': osm_type, 'osm_id': osm_id,
                          'name': name, 'locationLabel': p.get('display_name', ''),
                          'coordinates': [coordinates[1], coordinates[0]], 'placeType': p.get('type', ''),
                          'sourceUrl': f'https://www.openstreetmap.org/{osm_type}/{osm_id}', **extra})
        return items
