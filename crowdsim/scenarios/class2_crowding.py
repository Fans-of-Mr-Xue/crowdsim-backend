"""Isolated class-two user flow layout and SUMO demand.

No simulator is started by preparing a requirement or generating demand. Shared
hotspot settings are read-only and the class-one demand generator is not used.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import ClassVar
import xml.etree.ElementTree as ET

from crowdsim.domain.person_parameters import ITINERARY_LOCK_PARAM
from crowdsim.domain.requirement_spec import RequirementSpec
from crowdsim.scenarios.generated_hotspot_demand import HotspotDemandSpec

ROOT = Path(__file__).resolve().parents[2]
PRESET_PATH = ROOT / 'config/during_presets/memorial_tower_class2_flows_v1.json'
PRESET_ID = 'memorial-tower-class2-flows-v1'


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_preset():
    preset = json.loads(PRESET_PATH.read_text(encoding='utf-8'))
    if (preset['preset_id'] != PRESET_ID or preset['version'] != 1
            or preset['problem_type'] != 2):
        raise ValueError('unsupported class-two demo preset')
    resources = {name: _sha(ROOT / preset[name]) for name in
                 ('network', 'sumo_config', 'source_demand', 'requirement_template', 'flow_network')}
    fingerprint = hashlib.sha256(json.dumps(
        {'preset': preset, 'resources': resources}, sort_keys=True, separators=(',', ':')
    ).encode()).hexdigest()
    return preset, 'sha256:' + fingerprint, resources


def preset_capabilities():
    from .class2_flow_layout import flow_network
    preset, fingerprint, _ = load_preset()
    return {'id': PRESET_ID, 'version': 1, 'fingerprint': fingerprint,
            'problemType': 2, 'locationId': 'memorial-tower', 'name': preset['name'],
            'countMin': 1, 'countMax': 10000, 'requiresFlowDesign': True,
            'mapFingerprint': flow_network()['geometrySha256'],
            'departure': preset['departure'], 'requiresDatabase': False}


def prepare_requirement(body):
    from .class2_flow_layout import validate_design, build_flow_layout, flow_network, digest
    allowed = {'problemType', 'count', 'phenomenon', 'requestedLocation', 'seed', 'durationSeconds', 'source',
               'flowDesign', 'mapFingerprint', 'populationDistributions', 'metricIds', 'projectTitle'}
    if not isinstance(body, dict) or set(body)-allowed:
        raise ValueError('第二类人流请求包含不支持的字段')
    if type(body.get('problemType')) is not int or body['problemType'] != 2:
        raise ValueError('此入口仅支持第二类问题')
    phenomenon = body.get('phenomenon')
    if not isinstance(phenomenon, str) or not 1 <= len(phenomenon.strip()) <= 2000:
        raise ValueError('请填写研究目的')
    location = body.get('requestedLocation', '')
    if not isinstance(location, str) or not location.strip() or len(location) > 500:
        raise ValueError('请提供已确认的纪念塔研究地点')
    preset, fingerprint, resources = load_preset()
    count = body.get('count')
    design = validate_design(body.get('flowDesign'), count)
    if body.get('mapFingerprint') != flow_network()['geometrySha256']:
        raise ValueError('人流地图版本已变化，请刷新地图并重新确认方案')
    layout = build_flow_layout(design)  # Capacity and deterministic layout; no SUMO process.
    seed = body.get('seed', preset['default_seed'])
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ValueError('seed must be an integer between 0 and 2147483647')
    duration = body.get('durationSeconds')
    if type(duration) not in (int, float) or not 1 <= duration <= 1800 or not math.isfinite(duration):
        raise ValueError('durationSeconds must be between 1 and 1800')
    source = body.get('source')
    if (not isinstance(source, dict) or set(source) != {'conversationId', 'snapshotId'}
            or any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', value)
                   for value in source.values())):
        raise ValueError('source must identify one conversation and confirmed snapshot')
    metrics = body.get('metricIds')
    if not isinstance(metrics, list) or not metrics or len(metrics) != len(set(metrics)) or any(
            item not in {'global-density', 'local-density', 'global-speed', 'local-speed', 'boundary-density-difference'} for item in metrics):
        raise ValueError('请提供第二类方案中的有效关注指标')
    binding = {'preset_id': PRESET_ID, 'version': 1, 'fingerprint': fingerprint,
               'count': count, 'seed': seed, 'duration_seconds': duration,
               'phenomenon': phenomenon.strip(), 'requested_location': location, 'source': dict(source),
               'flow_design': design, 'flow_design_sha256': digest(design), 'layout_sha256': digest(layout),
               'map_fingerprint': flow_network()['geometrySha256'], 'resources': resources}
    requirement = deepcopy(json.loads((ROOT/preset['requirement_template']).read_text())['requirement'])
    requirement.update(schema_version=1, research={'problem_type': 2}, class2_demo=binding)
    requirement['project'] = {'title': body.get('projectTitle'), 'description': phenomenon.strip()}
    requirement['population'] = {'total': count, 'distributions': deepcopy(body.get('populationDistributions'))}
    requirement['observation']['metric_ids'] = list(metrics)
    requirement['strategy'] = {'phenomenon_source': phenomenon.strip(), 'generation_method': 'llm'}
    return RequirementSpec.parse(requirement)


def validate_binding(requirement):
    binding = requirement.get('class2_demo')
    if not isinstance(binding, dict) or requirement.get('research', {}).get('problem_type') != 2:
        raise ValueError('第二类人流执行记录无效')
    rebuilt = prepare_requirement({'problemType': 2, 'count': binding.get('count'),
        'phenomenon': binding.get('phenomenon'), 'requestedLocation': binding.get('requested_location', ''),
        'seed': binding.get('seed'), 'durationSeconds': binding.get('duration_seconds'),
        'source': binding.get('source'), 'flowDesign': binding.get('flow_design'),
        'mapFingerprint': binding.get('map_fingerprint'), 'projectTitle': requirement.get('project', {}).get('title'),
        'populationDistributions': requirement.get('population', {}).get('distributions'),
        'metricIds': requirement.get('observation', {}).get('metric_ids')})
    if binding != rebuilt.payload['class2_demo'] or requirement != rebuilt.payload:
        raise ValueError('第二类人流配置或路网资源已变化，请重新准备需求')
    return binding


def record_initial_state(runtime, directory):
    """Archive real READY coordinates separately from the requested layout."""
    state = {'run_id': runtime.run_id, 'time_seconds': runtime.time_seconds,
             'state': 'READY', 'population': runtime.population.diagnostics(),
             'class2_demo': runtime.requirement_record['requirement']['class2_demo'],
             'positions': [{'person_id': p.person_id, 'edge': p.edge_id,
                            'lane_position': p.lane_position, 'x': p.x, 'y': p.y,
                            'speed': p.speed} for p in runtime.current.persons.values()]}
    path = Path(directory)/'class2_initial_state.json'
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2)+'\n')
    runtime.recorder.update_manifest(class2_demo={
        **state['class2_demo'], 'initial_state_file': path.name,
        'initial_state_sha256': _sha(path), 'initial_time_seconds': runtime.time_seconds})


@dataclass(frozen=True)
class Class2CrowdingDemandSpec(HotspotDemandSpec):
    bound_count: int | None = None
    bound_seed: int | None = None
    expected_fingerprint: str | None = None
    flow_design: dict | None = None
    duration_seconds: float = 300
    is_class2_demo: ClassVar[bool] = True

    def configuration(self):
        preset, fingerprint, _ = load_preset()
        if self.expected_fingerprint is not None and fingerprint != self.expected_fingerprint:
            raise ValueError('第二类人流资源已变化')
        return preset, {'id': PRESET_ID, 'visitor_count': self.bound_count or 0}

    def validate_count(self, count):
        super().validate_count(count)
        if self.bound_count is not None and count != self.bound_count:
            raise ValueError('count differs from the frozen class-two requirement')

    def capabilities(self):
        return {**super().capabilities(), 'class2_demo': True, 'requires_flow_design': True}

    def generate(self, directory, network_path, requested_count=None, **kwargs):
        import sumolib
        from .class2_flow_layout import validate_design, build_flow_layout, digest, flow_network
        from crowdsim.decision.position_aware_router import EdgePosition, PositionAwarePedestrianRouter, PositionRouteUnavailable
        preset, _ = self.configuration()
        count = self.bound_count if requested_count is None else requested_count
        self.validate_count(count)
        if self.bound_seed is not None and self.seed != self.bound_seed:
            raise ValueError('seed differs from the frozen class-two requirement')
        if _sha(network_path) != _sha(ROOT/preset['network']):
            raise ValueError('class-two layout requires its frozen monument network')
        design = validate_design(self.flow_design, count)
        layout = build_flow_layout(design)
        requested_layout_sha = digest(layout)
        network = sumolib.net.readNet(str(network_path), withInternal=True, withPedestrianConnections=True)
        router = PositionAwarePedestrianRouter(network)
        root = ET.Element('routes')
        for flow in design['flows']:
            ET.SubElement(root, 'vType', {'id': 'class2.flow.' + flow['id'], 'vClass': 'pedestrian',
                'maxSpeed': '1e-9', 'desiredMaxSpeed': str(flow['speed']), 'speedFactor': '1', 'speedDev': '0'})
        for person in layout['positions']:
            target = person['destination']
            route = router.route(EdgePosition(person['edge'], person['position']),
                                 EdgePosition(target['edge'], target['position']))
            person['route'] = list(route.edges)
            element = ET.SubElement(root, 'person', {'id': person['person_id'], 'depart': '0',
                'type': 'class2.flow.' + person['flow_id'], 'speedFactor': '1',
                'departPos': f"{person['position']:.9f}"})
            for key, value in [(ITINERARY_LOCK_PARAM, 'true'), ('crowdsim.cohort', 'class2_flow'),
                               ('crowdsim.flow_id', person['flow_id']), ('crowdsim.flow_speed', str(person['speed']))]:
                ET.SubElement(element, 'param', {'key': key, 'value': value})
            ET.SubElement(element, 'walk', {'edges': ' '.join(route.edges), 'arrivalPos': f"{target['position']:.9f}",
                'departPosLat': f"{person['lateral']:.9f}"})
            length = network.getLane(target['source_lane_id']).getLength()
            guard_edge = target['edge']
            guard = .001 if target['position'] > length/2 else length-.001
            guard_route = (guard_edge,)
            if abs(guard-target['position']) < 2:
                for candidate in flow_network()['lanes']:
                    if candidate['sourceLength'] < 5:
                        continue
                    point = EdgePosition(candidate['edgeId'], candidate['sourceLength']/2)
                    try:
                        continuation = router.route(EdgePosition(target['edge'], target['position']), point)
                    except PositionRouteUnavailable:
                        continue
                    if continuation.distance_m >= 2:
                        guard_route, guard_edge, guard = continuation.edges, point.edge_id, point.offset_m
                        break
                else:
                    raise ValueError('目的地路段过短且缺少可达的停留保留行程，请调整终点')
            ET.SubElement(element, 'walk', {'edges': ' '.join(guard_route), 'arrivalPos': f'{guard:.9f}',
                'departPosLat': f"{person['lateral']:.9f}"})
            # Safety stage prevents deletion even for identical start/end points.
            # The flow controller keeps dwelling people in the walking model.
            guard_lane = next(lane for lane in network.getEdge(guard_edge).getLanes() if lane.allows('pedestrian'))
            # A finite duration keeps this guard beyond the simulation horizon
            # and can be copied by TraCI when a reviewed route is replaced.
            ET.SubElement(element, 'stop', {'lane': guard_lane.getID(),
                'endPos': f'{guard:.9f}', 'duration': str(self.duration_seconds + 2), 'actType': 'class2_destination'})
        directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
        output = directory/'class2.generated.rou.xml'
        ET.indent(root)
        ET.ElementTree(root).write(output, encoding='utf-8', xml_declaration=True)
        layout['count'], layout['seed'] = count, self.seed
        layout_path = directory/'class2_initial_layout.json'
        layout_path.write_text(json.dumps(layout, ensure_ascii=False, indent=2)+'\n')
        report = {'generation_version': 'class2-flows/1', 'initial_population': True,
            'hotspot_id': PRESET_ID, 'visitor_count': count, 'total_count': count, 'background_count': 0,
            'requested_count': requested_count, 'effective_count': count, 'seed': self.seed,
            'spawn_distribution': 'class2_user_flows', 'flow_counts': {f['id']: f['count'] for f in design['flows']},
            'spawn_edge_counts': dict(Counter(p['edge'] for p in layout['positions'])),
            'flow_design_sha256': digest(design), 'requested_layout_sha256': requested_layout_sha, 'layout_file': str(layout_path),
            'preset_fingerprint': load_preset()[1], 'source_sha256': _sha(self.source_path),
            'network_sha256': _sha(network_path), 'layout_sha256': _sha(layout_path),
            'departure': preset['departure'], 'output': str(output)}
        (directory/'demand_generation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        return output, report


def observation_map_geometry(network, geometry):
    """Geographic observation cells for the isolated class-two console only."""
    def coordinates(value):
        if len(value) >= 2 and all(isinstance(v, (float, int)) for v in value[:2]):
            return list(network.xy_to_lonlat(value[0], value[1]))
        return [coordinates(item) for item in value]

    def geographic_geometry(value):
        if value['type'] == 'GeometryCollection':
            return {'type': 'GeometryCollection',
                    'geometries': [geographic_geometry(item) for item in value['geometries']]}
        return {'type': value['type'], 'coordinates': coordinates(value['coordinates'])}

    def geographic(item):
        return {**item, 'map_geometry': geographic_geometry(item['geometry'])}

    return {**geometry, 'map_crs': 'EPSG:4326',
            'cells': [geographic(item) for item in geometry['cells']],
            'boundaries': [geographic(item) for item in geometry['boundaries']]}
