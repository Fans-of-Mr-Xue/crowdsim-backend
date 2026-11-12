"""Read-only geographic overlays for a frozen class-two run."""
from types import SimpleNamespace

from crowdsim.decision.position_aware_router import EdgePosition
from .class2_flow_layout import flow_network, position
from .class2_map_locations import _route_path
from .class2_control_context import _junction
from .class2_region_connections import scene_region_connections


def build_map_geometry(runtime, layout):
    data = flow_network()
    lanes = {lane['id']: lane for lane in data['lanes']}
    network = runtime.network

    def geographic(points):
        return [list(network.xy_to_lonlat(x, y)) for x, y in points]

    edges = {}
    for lane in lanes.values():
        edges.setdefault(lane['edgeId'], []).append(geographic(
            [(x + data['originXY'][0], y + data['originXY'][1]) for x, y in lane['path']]))
    flows = []
    for flow in layout['flow_design']['flows']:
        endpoints = [position(lanes[flow[k]['lane_id']], flow[k]['offset']) for k in ('origin', 'destination')]
        start, end = [EdgePosition(p['edge'], p['position']) for p in endpoints]
        route = runtime.route_provider.position_router.route(start, end)
        full_path = geographic(_route_path(network.net, route, start, end, (0, 0)))
        segments = []
        for i, edge_id in enumerate(route.edges):
            edge = network.edges[edge_id]
            begin = edge.getFromNode().getID()
            entered = _junction(network.edges[route.edges[i - 1]], edge) if i else None
            leaving = _junction(edge, network.edges[route.edges[i + 1]]) if i + 1 < len(route.edges) else None
            a = start.offset_m if i == 0 else 0 if entered == begin else edge.getLength()
            b = end.offset_m if i == len(route.edges) - 1 else 0 if leaving == begin else edge.getLength()
            if abs(b - a) < 1e-8:
                continue
            path = _route_path(network.net, SimpleNamespace(edges=(edge_id,)),
                               EdgePosition(edge_id, a), EdgePosition(edge_id, b), (0, 0))
            segments.append({'edgeId': edge_id, 'direction': 'forward' if b > a else 'reverse',
                             'path': geographic(path)})
        flows.append({'id': flow['id'], 'origin': geographic([(endpoints[0]['x'], endpoints[0]['y'])])[0],
                      'destination': geographic([(endpoints[1]['x'], endpoints[1]['y'])])[0],
                      'path': full_path, 'segments': segments})
    binding = runtime.requirement_record['requirement']['class2_demo']
    return {'schemaVersion': 1, 'coordinateSystem': 'wgs84', 'runId': runtime.run_id,
            'source': dict(binding['source']), 'networkSha256': data['sourceSha256'],
            'flowDesignSha256': layout['flow_design_sha256'], 'flows': flows,
            'edges': [{'edgeId': edge, 'paths': paths} for edge, paths in edges.items()],
            **scene_region_connections(network, data)}
