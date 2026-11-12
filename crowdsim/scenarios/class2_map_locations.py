"""Read-only Memorial map tools. No database, model, TraCI or SUMO process."""
from collections import OrderedDict
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import math
import re
import threading
import time
import xml.etree.ElementTree as ET

import sumolib
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import substring

from .class2_flow_layout import ROOT, flow_network, position, validate_design, build_flow_layout
from crowdsim.decision.position_aware_router import EdgePosition, PositionAwarePedestrianRouter

DIRECTIONS = {'东北': (1, 1), '西北': (-1, 1), '东南': (1, -1), '西南': (-1, -1),
              '北': (0, 1), '南': (0, -1), '东': (1, 0), '西': (-1, 0)}


@lru_cache(maxsize=2)
def _geometry(version):
    data = flow_network()
    net = sumolib.net.readNet(str(ROOT / 'scenarios/shanghai_bund/bund.net.xml'),
                             withInternal=True, withPedestrianConnections=True)
    nodes, monument = {}, None
    for _, item in ET.iterparse(ROOT / 'scenarios/shanghai_bund/bund_real.osm.xml', events=('end',)):
        if item.tag == 'node':
            nodes[item.get('id')] = (float(item.get('lon')), float(item.get('lat')))
        elif item.tag == 'way' and item.get('id') == '47007059':
            monument = [nodes[n.get('ref')] for n in item.findall('nd')]
    origin = data['originXY']
    outline = [[x-origin[0], y-origin[1]] for x, y in
               (net.convertLonLat2XY(*point) for point in monument)] if monument else []
    center = list(Polygon(outline).centroid.coords)[0] if outline else data['center']
    return data, net, PositionAwarePedestrianRouter(net), Polygon(data['scope']), center, outline


def _source(body):
    data = flow_network()
    if body.get('locationId') != 'memorial-tower' or body.get('networkSha256') != data['sourceSha256']:
        raise ValueError('此工具仅支持已确认的纪念塔场景，路网版本须与当前地图一致。')
    source = body.get('source')
    if (not isinstance(source, dict) or set(source) != {'conversationId', 'draftId', 'revision'}
            or any(not isinstance(source[k], str) or not re.fullmatch(r'[\w-]{1,120}', source[k])
                   for k in ('conversationId', 'draftId'))
            or type(source['revision']) is not int or source['revision'] < 0):
        raise ValueError('地图查询必须绑定当前会话、草稿和需求版本。')
    return source


def _intent(text, polygon, center):
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 120:
        raise ValueError('位置描述须为1～120字。')
    if re.search(r'入口|出口|门|地下|楼|大厦|博物馆|咖啡|餐厅|停车|厕所|卫生间|广场|江边|江畔|河边|水中|江中|桥上|马路', text):
        raise ValueError('当前没有已核验的地面出入口或这些设施的可用标点；请改用纪念塔／公园的方位步道，或手动标点。')
    words = re.sub(r'人民英雄纪念塔|黄浦公园|人民英雄|纪念塔|纪念碑|公园', '', text)
    words = re.sub(r'中心(?=[东西南北])|大约|约|左右|附近|周边|步行|步道|道路|环形|环道|区域|位置|靠近|一带|范围|北|南|东|西|侧|方|部|端|面|边|角|的|在|从|到|内|外|旁|上|处|段', '', words)
    words = re.sub(r'\d+(?:\.\d+)?\s*(?:米|m)', '', words)
    if re.search(r'[\w\u4e00-\u9fff]', words):
        raise ValueError('此位置包含尚未核验的地名或描述，请使用纪念塔／黄浦公园的明确方位步道。')
    direction = None
    remaining = text
    for name in DIRECTIONS:
        pattern = re.escape(name)+r'(?=侧|方|部|端|面|边|角|步道|道路|$)'
        if re.search(pattern, remaining):
            if direction is not None:
                raise ValueError('一个位置描述包含多个方位，请分别描述起点和终点。')
            direction = name
            remaining = re.sub(pattern, '', remaining)
    if not any(word in text for word in ('纪念塔', '纪念碑', '人民英雄', '公园')) and not direction:
        raise ValueError('第一版支持纪念塔／黄浦公园及其东、西、南、北和四个斜向方位；其他地名尚未核验。')
    landmark = '纪念塔' if any(word in text for word in ('纪念塔', '纪念碑', '人民英雄')) else '黄浦公园'
    xmin, ymin, xmax, ymax = polygon.bounds
    base = center if landmark == '纪念塔' else polygon.centroid.coords[0]
    vector = DIRECTIONS.get(direction, (0, 0))
    magnitude = math.hypot(*vector) or 1
    unit = (vector[0]/magnitude, vector[1]/magnitude)
    distance = re.search(r'(\d+(?:\.\d+)?)\s*(?:米|m)', text)
    reach = float(distance[1]) if distance else 23 if landmark == '纪念塔' else min(xmax-xmin, ymax-ymin)*.38
    if distance and (not direction or not 1 <= reach <= 60):
        raise ValueError('带距离的标点须明确方位，且距离在1～60米内。')
    goal = (base[0]+unit[0]*reach, base[1]+unit[1]*reach)
    return {'landmark': landmark, 'direction': direction, 'base': base, 'unit': unit, 'goal': goal,
            'maxSnapM': 12 if distance else None,
            'label': landmark + (direction+('侧约'+format(reach, 'g')+'米步道' if distance else '侧步道') if direction else '附近步道')}


def _in_region(point, intent, tolerance=1):
    if not intent['direction']:
        return True
    dx, dy = point[0]-intent['base'][0], point[1]-intent['base'][1]
    ux, uy = intent['unit']
    if ux and dx*math.copysign(1,ux) < -tolerance or uy and dy*math.copysign(1,uy) < -tolerance:
        return False
    forward = dx*ux+dy*uy
    sideways = abs(dx*uy-dy*ux)
    return forward >= -tolerance and sideways <= max(18, forward*1.8)+tolerance


def _route_path(net, route, start, end, origin):
    """Orient and trim the actual route edges, keeping local-metre geometry."""
    states = [(0.0, [])]
    for index, edge_id in enumerate(route.edges):
        edge = net.getEdge(edge_id)
        lane = next(l for l in edge.getLanes() if l.allows('pedestrian'))
        line = LineString(lane.getShape())
        length = min(float(edge.getLength()), float(lane.getLength()))
        first = start.offset_m/length*line.length if index == 0 else None
        last = end.offset_m/length*line.length if index == len(route.edges)-1 else None
        options = [substring(line, first, last)] if first is not None and last is not None else (
            [substring(line, first, 0), substring(line, first, line.length)] if first is not None else
            [substring(line, 0, last), substring(line, line.length, last)] if last is not None else
            [line, LineString(list(line.coords)[::-1])])
        choices = []
        for segment in options:
            points = list(segment.coords)
            cost, previous = min(states, key=lambda s: s[0] + (math.dist(s[1][-1], points[0]) if s[1] else 0))
            gap = math.dist(previous[-1], points[0]) if previous else 0
            choices.append((cost+gap+segment.length, previous+points))
        states = choices
    points = min(states, key=lambda s: s[0])[1]
    result = []
    for x, y in points:
        point = [round(x-origin[0], 4), round(y-origin[1], 4)]
        if not result or point != result[-1]:
            result.append(point)
    return result


class MemorialMapTool:
    def __init__(self):
        self.candidates = OrderedDict()
        self.lock = threading.RLock()

    def context(self, body):
        _source(body)
        data, _, _, polygon, center, outline = _geometry(flow_network()['geometrySha256'])
        return {'locationId': 'memorial-tower', 'networkSha256': data['sourceSha256'],
                'geometrySha256': data['geometrySha256'], 'walkableParts': len(data['lanes']),
                'landmarks': [{'name': '人民英雄纪念塔', 'aliases': ['纪念塔', '纪念碑'], 'center': list(center)},
                              {'name': '黄浦公园', 'center': list(polygon.centroid.coords)[0]}],
                'monumentOutline': outline, 'directions': list(DIRECTIONS),
                'rules': ['仅使用当前范围内步行路段；背景道路不是可标点道路。',
                          '入口和地下设施尚未核验，不据方位虚构入口。',
                          '方位距离从对应地标center计算，支持1～60米，匹配步道距目标点最多12米；不可擅自删改距离约束。',
                          '候选只用于本会话当前草稿版本；写入前须通过整组路线与面积预览校验。'],
                'maxSpreadM': 45}

    def navigation(self, body):
        """Plan anchor-to-anchor walking routes without decisions or layout allocation."""
        with self.lock:
            source = _source(body)
            data, net, router, polygon, _, _ = _geometry(flow_network()['geometrySha256'])
            design = body.get('design')
            if (not isinstance(design, dict) or type(design.get('version')) is not int or design['version'] != 1
                    or design.get('location_id') != 'memorial-tower'
                    or design.get('network_sha256') != data['sourceSha256']
                    or not isinstance(design.get('flows'), list) or len(design['flows']) > 20):
                raise ValueError('导航预览须使用当前纪念塔人流配置。')
            lanes = {lane['id']: lane for lane in data['lanes']}
            ids, routes, issues = set(), [], []
            for flow in design['flows']:
                if (not isinstance(flow, dict) or not isinstance(flow.get('id'), str)
                        or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', flow['id']) or flow['id'] in ids):
                    raise ValueError('导航预览的人流编号无效或重复。')
                ids.add(flow['id'])
                try:
                    for kind in ('origin', 'destination'):
                        anchor = flow.get(kind)
                        if anchor is None:
                            continue
                        if (not isinstance(anchor, dict) or set(anchor) != {'lane_id', 'offset'}
                                or anchor.get('lane_id') not in lanes
                                or type(anchor.get('offset')) not in (int, float) or not math.isfinite(anchor['offset'])
                                or not 0 <= anchor['offset'] <= lanes[anchor['lane_id']]['length']+.001):
                            raise ValueError('起终点不在当前可步行路网上。')
                    if flow.get('origin') is None or flow.get('destination') is None:
                        continue
                    a, b = [position(lanes[flow[kind]['lane_id']], min(flow[kind]['offset'], lanes[flow[kind]['lane_id']]['length']))
                            for kind in ('origin', 'destination')]
                    start, end = EdgePosition(a['edge'], a['position']), EdgePosition(b['edge'], b['position'])
                    route = router.route(start, end)
                    path = _route_path(net, route, start, end, data['originXY'])
                    if len(path) < 2 or not polygon.buffer(.1).covers(LineString(path)):
                        raise ValueError('步行路线越出研究范围或起终点重合，请调整标点。')
                    routes.append({'flowId': flow['id'], 'path': path, 'distanceM': round(route.distance_m, 2)})
                except (ValueError, KeyError, TypeError) as exc:
                    issues.append(f"{str(flow.get('name') or '人流')[:40]}：{exc}")
            return {'routes': routes, 'issues': issues, 'geometrySha256': data['geometrySha256'],
                    'revision': source['revision']}

    def resolve(self, body):
        with self.lock:
            source = _source(body)
            data, _, _, polygon, center, _ = _geometry(flow_network()['geometrySha256'])
            queries = body.get('queries')
            if not isinstance(queries, list) or not 1 <= len(queries) <= 40:
                raise ValueError('请一次提供1～40个起终点查询。')
            results, keys = [], set()
            for query in queries:
                if (not isinstance(query, dict) or set(query) != {'key', 'description'}
                        or not isinstance(query['key'], str) or not re.fullmatch(r'[\w.-]{1,80}', query['key']) or query['key'] in keys):
                    raise ValueError('每个位置查询须有唯一key和description。')
                keys.add(query['key'])
                try:
                    intent = _intent(query['description'], polygon, center)
                except ValueError as exc:
                    results.append({'key': query['key'], 'description': query['description'], 'candidates': [], 'reason': str(exc)})
                    continue
                scored = []
                for lane in data['lanes']:
                    for i in range(max(1, math.ceil(lane['length']/6))):
                        offset = lane['length']*(i+.5)/max(1, math.ceil(lane['length']/6))
                        p = position(lane, offset)
                        xy = (p['local_x'], p['local_y'])
                        if polygon.covers(Point(xy)) and _in_region(xy, intent, 0):
                            scored.append((math.dist(xy, intent['goal']), lane, offset, xy))
                selected = []
                for score, lane, offset, xy in sorted(scored, key=lambda item: item[0]):
                    if intent['maxSnapM'] is not None and score > intent['maxSnapM']:
                        continue
                    if any(math.dist(xy, previous['point']) < 9 for previous in selected):
                        continue
                    anchor = {'lane_id': lane['id'], 'offset': offset}
                    ref = 'map-' + hashlib.sha256(json.dumps([source, data['geometrySha256'], query, anchor], sort_keys=True).encode()).hexdigest()[:24]
                    candidate = {'ref': ref, 'label': intent['label'], 'anchor': anchor, 'point': list(xy),
                                 'distanceToLandmarkM': round(math.dist(xy, intent['base']), 1), 'widthM': lane['width']}
                    selected.append(candidate)
                    self.candidates[ref] = {'source': deepcopy(source), 'map': data['geometrySha256'],
                        'anchor': deepcopy(anchor), 'intent': intent, 'expires': time.monotonic()+900}
                    self.candidates.move_to_end(ref)
                    if len(selected) == 3:
                        break
                results.append({'key': query['key'], 'description': query['description'], 'candidates': selected,
                                'reason': '' if selected else '当前范围内没有匹配方位的步道。'})
            while len(self.candidates) > 4096:
                self.candidates.popitem(last=False)
            return {'results': results, 'networkSha256': data['sourceSha256'], 'geometrySha256': data['geometrySha256'],
                    'revision': source['revision']}

    def preview(self, body):
        with self.lock:
            source = _source(body)
            data, net, router, polygon, _, _ = _geometry(flow_network()['geometrySha256'])
            issues, checks, routes = [], [], []
            try:
                design = validate_design(body.get('design'))
                layout = build_flow_layout(design)
                refs = body.get('pointRefs', [])
                if not isinstance(refs, list) or len(refs) > 40:
                    raise ValueError('点位引用格式无效。')
                intents = {}
                for item in refs:
                    if not isinstance(item, dict) or set(item) != {'flowId', 'kind', 'ref'} or item['kind'] not in ('origin', 'destination'):
                        raise ValueError('点位引用格式无效。')
                    candidate = self.candidates.get(item['ref'])
                    if not candidate or candidate['expires'] < time.monotonic() or candidate['source'] != source or candidate['map'] != data['geometrySha256']:
                        raise ValueError('候选点已过期或不属于当前会话／需求版本，请重新解析。')
                    flow = next((f for f in design['flows'] if f['id'] == item['flowId']), None)
                    if flow is None or flow[item['kind']] != candidate['anchor']:
                        raise ValueError('候选点与本次人流标记不一致。')
                    intents[(item['flowId'], item['kind'])] = candidate['intent']
                by_lane = {lane['id']: lane for lane in data['lanes']}
                for flow in layout['flows']:
                    areas = {}
                    for kind in ('origin', 'destination'):
                        footprint = flow[kind+'_layout']
                        areas[kind] = round(footprint['area'], 3)
                        anchor = position(by_lane[flow[kind]['lane_id']], flow[kind]['offset'])
                        center = (anchor['local_x'], anchor['local_y'])
                        points = [(p['local_x'], p['local_y']) for p in footprint['points']]
                        if any(math.dist(p, center) > 45 for p in points):
                            raise ValueError(f"{flow['name']}：{kind}布置延伸超过45米，请提高密度、减少人数或调整位置。")
                        intent = intents.get((flow['id'], kind))
                        if intent and any(not _in_region(p, intent) for p in points):
                            raise ValueError(f"{flow['name']}：布置范围超出所描述方位，请选择更合适的候选点或调整人数／密度。")
                    checks.append({'flowId': flow['id'], 'name': flow['name'], 'count': flow['count'],
                        'density': flow['density'], 'speed': flow['speed'], 'requiredAreaM2': flow['count']/flow['density'],
                        'originLabel': intents.get((flow['id'],'origin'),{}).get('label','已有起点'),
                        'destinationLabel': intents.get((flow['id'],'destination'),{}).get('label','已有终点'),
                        'originAreaM2': areas['origin'], 'destinationAreaM2': areas['destination']})
                    start = position(by_lane[flow['origin']['lane_id']], flow['origin']['offset'])
                    end = position(by_lane[flow['destination']['lane_id']], flow['destination']['offset'])
                    a, b = EdgePosition(start['edge'], start['position']), EdgePosition(end['edge'], end['position'])
                    route = router.route(a, b)
                    path = _route_path(net, route, a, b, data['originXY'])
                    if len(path) < 2 or not polygon.buffer(.1).covers(LineString(path)):
                        raise ValueError(f"{flow['name']}：步行路线越出研究范围或起终点重合，请调整标点。")
                    distances = []
                    for person in (p for p in layout['positions'] if p['flow_id'] == flow['id']):
                        target = person['destination']
                        source_position = EdgePosition(person['edge'], person['position'])
                        target_position = EdgePosition(target['edge'], target['position'])
                        walking = router.route(source_position, target_position)
                        person_path = _route_path(net, walking, source_position, target_position, data['originXY'])
                        if len(person_path)>1 and not polygon.buffer(.1).covers(LineString(person_path)):
                            raise ValueError(f"{flow['name']}：分布后有行人的路线越出研究范围，请调整标点。")
                        distances.append(walking.distance_m)
                    routes.append({'flowId': flow['id'], 'path': path, 'distanceM': round(route.distance_m, 2),
                        'verifiedPeople': len(distances), 'minDistanceM': round(min(distances), 2),
                        'maxDistanceM': round(max(distances), 2), 'edgeIds': list(route.edges)})
            except (ValueError, KeyError, TypeError) as exc:
                issues.append(str(exc))
            return {'valid': not issues, 'issues': issues, 'checks': checks, 'routes': routes,
                    'geometrySha256': data['geometrySha256'], 'revision': source['revision'],
                    'summary': '路线和布置面积校验通过，未启动仿真。' if not issues else '未通过校验：'+'；'.join(issues)}


def register_map_location_routes(app):
    import asyncio
    from aiohttp import web
    service = MemorialMapTool()
    for path, operation in [('map-context', service.context), ('resolve-locations', service.resolve),
                            ('placement-preview', service.preview), ('navigation-preview', service.navigation)]:
        async def handle(request, operation=operation):
            try:
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError('地图请求必须为对象。')
                return web.json_response(await asyncio.to_thread(operation, body))
            except (ValueError, KeyError, TypeError) as exc:
                return web.json_response({'type': 'error', 'code': 'invalid_class2_map', 'message': str(exc)}, status=400)
        app.router.add_post('/api/v1/during/class2/'+path, handle)
