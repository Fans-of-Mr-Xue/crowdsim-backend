"""Sample F00 people with the during profile and hotspot distributions."""
from dataclasses import asdict
from functools import lru_cache
import hashlib
import math
import random
import xml.etree.ElementTree as ET

from postanalysis_api.schemas.common import ApiError
from postanalysis_api.services.factual_context import spatial_cohorts
from .memorial_scenario import NET_FILE, ENTRY_EDGES, control_schedule


@lru_cache(maxsize=1)
def network():
    import sumolib
    return sumolib.net.readNet(str(NET_FILE), withInternal=True, withPedestrianConnections=True)


def compile_distribution(dataset, seed, actions, duration, output):
    from sumolib.geomhelper import polygonOffsetAndDistanceToPoint, positionAtShapeOffset, rotationAtShapeOffset
    from crowdsim.domain.population_profiles import PopulationProfileSampler
    from crowdsim.decision.position_aware_router import PositionAwarePedestrianRouter, EdgePosition
    from crowdsim.scenarios.hotspot_demand import _viewing_zone_specs, _viewing_zone_schedule
    context = dataset['metadata']['factualContext']
    from postanalysis_api.services.population_execution import prepare_execution_dataset
    prepare_execution_dataset(dataset)
    if hashlib.sha256(NET_FILE.read_bytes()).hexdigest() != context['resourceSha256']['scenarios/shanghai_bund/bund.net.xml']:
        raise ApiError('GEOMETRY_VERSION_MISMATCH', '路网已变化，请从原归档重新导入并核验空间对应。', 422)
    controls = control_schedule(actions, dataset['metadata']['executionProfile']['baselineControl'])
    people = spatial_cohorts(dataset)
    sampler = PopulationProfileSampler(seed=seed)
    sampler.config = context['populationProfiles']
    sampler.configure_distributions(context.get('populationDistributions', {}))
    sampler.prepare_population(person['id'] for person in people)
    hotspot = context['hotspot']
    zones = _viewing_zone_specs(network(), hotspot, tuple(hotspot['target_edges']))
    targets = _viewing_zone_schedule(zones, len(people), rng_seed=seed)
    rng, router = random.Random(seed), PositionAwarePedestrianRouter(network())
    root, manifest = ET.Element('routes'), []
    supports = {}
    for person, (hold_edge, hold_position, zone_id) in zip(people, targets):
        if 'edgeId' not in person:
            # Trajectories are optional. Sample legal road length within the observed cell.
            from shapely.geometry import LineString, shape
            cell_id = person['regionId']
            if cell_id not in supports:
                cell = next(cell for cell in context['geometry']['cells'] if cell['cell_id'] == cell_id)
                polygon = shape(cell['geometry'])
                lines = []
                for edge in network().getEdges():
                    for lane in edge.getLanes():
                        if lane.allows('pedestrian'):
                            clipped = LineString(lane.getShape()).intersection(polygon)
                            parts = list(clipped.geoms) if hasattr(clipped, 'geoms') else [clipped]
                            lines.extend((edge.getID(), part) for part in parts if part.geom_type == 'LineString' and part.length > 0)
                supports[cell_id] = lines
            lines = supports[cell_id]
            if not lines:
                raise ApiError('INITIAL_POSITION_UNSUPPORTED', f'原网格{cell_id}缺少可行走生成空间，不能转移到默认入口。', 422)
            edge_id, line = rng.choices(lines, weights=[line.length for _, line in lines], k=1)[0]
            point = line.interpolate(rng.random() * line.length)
            person.update(edgeId=edge_id, x=point.x, y=point.y)
        profile = sampler.sample_profile(person['id'])
        edge = network().getEdge(person['edgeId'])
        lanes = [lane for lane in edge.getLanes() if lane.allows('pedestrian')]
        if not lanes or edge.getFunction() in {'walkingarea', 'crossing'}:
            raise ApiError('INITIAL_POSITION_UNSUPPORTED', '初始道路无法直接插入行人，需要显式空间适配。', 422)
        lane = min(lanes, key=lambda value: polygonOffsetAndDistanceToPoint((person['x'], person['y']), value.getShape())[1])
        offset, _distance = polygonOffsetAndDistanceToPoint((person['x'], person['y']), lane.getShape())
        # SUMO chooses a walking stripe from route direction. Leave room inside
        # the SAME observed cell so that stripe choice cannot change its quota.
        from shapely.geometry import LineString, Point, shape
        from shapely.ops import nearest_points
        source_cell = next(cell for cell in context['geometry']['cells'] if cell['cell_id'] == person['regionId'])
        safe = shape(source_cell['geometry']).buffer(-lane.getWidth() / 2)
        safe_path = LineString(lane.getShape()).intersection(safe)
        if not safe_path.is_empty:
            point = nearest_points(Point(person['x'], person['y']), safe_path)[1]
            offset, _distance = polygonOffsetAndDistanceToPoint((point.x, point.y), lane.getShape())
        offset = min(router.edge_lengths[person['edgeId']], max(0, offset))
        center = positionAtShapeOffset(lane.getShape(), offset)
        angle = rotationAtShapeOffset(lane.getShape(), offset)
        lateral = ((person['y'] - center[1]) * math.cos(angle) -
                   (person['x'] - center[0]) * math.sin(angle)) if angle is not None else 0
        hold_lane = next(lane.getID() for lane in network().getEdge(hold_edge).getLanes() if lane.allows('pedestrian'))
        release = hotspot['activity_window_seconds'][1] + rng.uniform(*hotspot['visitor_release_delay_seconds'])
        follow = rng.random()
        destination = person['edgeId']
        if follow < controls['compliance'] and actions:
            destination = ENTRY_EDGES['C']
            release = max(controls['gateOpenSeconds'], controls['guidanceStartSeconds'] + controls['guidanceDelaySeconds'])
        source = EdgePosition(person['edgeId'], offset)
        target = EdgePosition(hold_edge, hold_position)
        approach = router.route(source, target)
        exit_position = router.edge_lengths[destination] / 2
        departure = router.route(target, EdgePosition(destination, exit_position))
        type_id = person['id'] + '_type'
        ET.SubElement(root, 'vType', id=type_id, vClass='pedestrian',
                      maxSpeed=f'{profile.free_walking_speed * profile.mobility:.6f}')
        node = ET.SubElement(root, 'person', id=person['id'], type=type_id, depart='0',
                             departPos=f'{offset:.6f}', departPosLat=f'{lateral:.6f}')
        ET.SubElement(node, 'walk', edges=' '.join(approach.edges), arrivalPos=f'{hold_position:.6f}')
        ET.SubElement(node, 'stop', lane=hold_lane, endPos=f'{hold_position:.6f}', posLat='0', until=f'{release:.6f}')
        ET.SubElement(node, 'walk', edges=' '.join(departure.edges), arrivalPos=f'{exit_position:.6f}')
        manifest.append({**person, 'entryPosition': offset, 'release': release, 'exitRegionId': destination,
                         'holdingEdge': hold_edge, 'holdingPosition': hold_position, 'viewingZoneId': zone_id,
                         'profile': asdict(profile)})
    # SUMO requires every vType to precede the persons that use it.
    root[:] = [*root.findall('vType'), *root.findall('person')]
    output.write_bytes(ET.tostring(root, encoding='utf-8', xml_declaration=True))
    return {'controls': controls, 'cohortCount': len(manifest), 'cohorts': manifest,
            'factualContext': {key: value for key, value in context.items() if key != 'initialDistribution'},
            'reconstruction': dataset['metadata']['reconstruction'],
            'assumptions': ['首帧原空间分布初始化；个体属性复用事中分布采样器，跨实验使用共同种子',
                            '网格边缘初始位置在原道路和原网格内预留条带宽度，避免插入时跨格',
                            '热点位置与活动释放按当前事中配置分布生成，历史隐藏行为仍需拟合验证',
                            '归档未证明事实控制，不自动施加开门或引导；无额外控制为显式模型假设',
                            '只使用首帧轨迹定位，不回放或逐帧强制匹配旧轨迹']}


def sumo_parameters(context):
    """The same server-owned striping parameters used by the during runtime."""
    sumo, striping = context['sumo'], context['sumo']['striping']
    values = {'--step-length': context['stepSeconds'], '--pedestrian.model': sumo['pedestrian_model'],
              '--time-to-teleport': sumo['time_to_teleport_seconds'],
              '--ignore-route-errors': str(sumo['ignore_route_errors']).lower(),
              '--pedestrian.striping.stripe-width': striping['stripe_width_meters'],
              '--pedestrian.striping.reserve-oncoming': striping['reserve_oncoming_fraction'],
              '--pedestrian.striping.reserve-oncoming.junctions': striping['reserve_oncoming_junctions_fraction'],
              '--pedestrian.striping.jamtime': striping['jamtime_seconds'],
              '--pedestrian.striping.jamtime.crossing': striping['jamtime_crossing_seconds'],
              '--pedestrian.striping.jamtime.narrow': striping['jamtime_narrow_seconds']}
    return [str(item) for pair in values.items() for item in pair]
