"""Random background trips within the existing memorial study map."""
import random
from .class2_flow_layout import flow_network, position
from crowdsim.decision.position_aware_router import EdgePosition, PositionRouteUnavailable

BACKGROUND_FLOW = '__background.random__'
DWELL_SECONDS = 5


def random_point(rng):
    lanes = [lane for lane in flow_network()['lanes'] if lane['length'] > 2]
    lane = rng.choices(lanes, weights=[lane['length'] * lane['width'] for lane in lanes])[0]
    return position(lane, rng.uniform(.5, lane['length'] - .5))


def next_trip(router, rng, source):
    for _ in range(200):
        target = random_point(rng)
        try:
            route = router.route(EdgePosition(source['edge'], source['position']),
                                 EdgePosition(target['edge'], target['position']))
        except PositionRouteUnavailable:
            continue
        if route.distance_m >= 2:
            return target, route
    # A finite number of area-weighted draws can miss a small disconnected
    # walkway entirely. Failure to sample it is not evidence of no route.
    # Inspect every study lane before declaring the trip impossible.
    lanes = [lane for lane in flow_network()['lanes'] if lane['length'] > 2]
    rng.shuffle(lanes)
    for lane in lanes:
        for offset in (rng.uniform(.5, lane['length'] - .5), .05, lane['length'] - .05):
            target = position(lane, offset)
            try:
                route = router.route(EdgePosition(source['edge'], source['position']),
                                     EdgePosition(target['edge'], target['position']))
            except PositionRouteUnavailable:
                continue
            # Short but valid trips are allowed on isolated short walkways.
            # Stay beyond the controller's .06m arrival tolerance.
            if route.distance_m > .1:
                return target, route
    raise ValueError('研究范围内未找到可达的背景行人随机目的地')


def add_background(layout, count, seed, router):
    rng = random.Random(seed)
    for index in range(count):
        source = random_point(rng)
        target, route = next_trip(router, rng, source)
        layout['positions'].append({
            'person_id': f'class2.{BACKGROUND_FLOW}.{index:05d}', 'flow_id': BACKGROUND_FLOW,
            'cohort': 'background', 'speed': 1.3, **source,
            'destination': target, 'route': list(route.edges)})
    return layout


class BackgroundTripPlanner:
    """Per-run reachable destinations using the existing position router."""

    def __init__(self, router):
        self.router = router
        self.lanes = [lane for lane in flow_network()['lanes'] if lane['length'] > 2]
        # Use the router's exact bidirectional pedestrian topology, including
        # internal connections. Geometric proximity does not imply reachability.
        components = {}
        for node in router.adjacency:
            if node in components:
                continue
            component = len(components)
            components[node] = component
            pending = [node]
            while pending:
                for arc in router.adjacency[pending.pop()]:
                    if arc.to not in components:
                        components[arc.to] = component
                        pending.append(arc.to)
        self.components = components
        self.lanes_by_component = {}
        for lane in self.lanes:
            component = components[(lane['edgeId'], 0)]
            self.lanes_by_component.setdefault(component, []).append(lane)

        # Two endpoint trees cover all continuous destination positions on a
        # lane. Keep every study-area tree and prepare them once before READY,
        # so subsequent trips do not search the full network inside a step.
        router.cache_limit = max(router.cache_limit, 2 * len(self.lanes))
        for lane in self.lanes:
            point = position(lane, lane['length'] / 2)
            endpoint = EdgePosition(point['edge'], point['position'])
            router.route(endpoint, endpoint)

    def next_trip(self, rng, source):
        component = self.components.get((source['edge'], 0))
        lanes = self.lanes_by_component.get(component, ())
        ranges = []
        for lane in lanes:
            lo, hi = .5, lane['length'] - .5
            pieces = [(lo, hi)]
            if lane['edgeId'] == source['edge']:
                start = position(lane, 0)['position']
                scale = (position(lane, lane['length'])['position'] - start) / lane['length']
                origin = (source['position'] - start) / scale
                # Remove a small interval around the current position rather
                # than repeatedly drawing a destination that is already reached.
                gap = .1 / abs(scale)
                pieces = [(lo, min(hi, origin-gap)), (max(lo, origin+gap), hi)]
            ranges.extend((lane, a, b) for a, b in pieces if b-a > 1e-6)
        if not ranges:
            raise ValueError('研究范围内未找到可达的背景行人随机目的地')
        lane, lo, hi = rng.choices(ranges, weights=[(b-a)*lane['width'] for lane, a, b in ranges])[0]
        target = position(lane, rng.uniform(lo, hi))
        route = self.router.route(EdgePosition(source['edge'], source['position']),
                                  EdgePosition(target['edge'], target['position']))
        return target, route
