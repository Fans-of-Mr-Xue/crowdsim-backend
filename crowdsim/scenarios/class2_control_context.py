"""Bounded route and free-flow overlap evidence for second-class decisions."""
from collections import Counter, defaultdict


def _ends(edge):
    return edge.getFromNode().getID(), edge.getToNode().getID()


def _junction(left, right):
    common = set(_ends(left)) & set(_ends(right))
    return next(iter(common)) if len(common) == 1 else None


def _passages(runtime, motion, route, destination, speed):
    """Estimate passage time using actual route geometry and endpoint offsets."""
    network = runtime.network.edges
    edges = [network[edge] for edge in route]
    time = runtime.time_seconds
    for index, edge in enumerate(edges):
        begin, end = _ends(edge)
        length = edge.getLength()
        if len(edges) == 1:
            source, target = motion.lane_position, destination['position']
        else:
            entered = _junction(edges[index - 1], edge) if index else None
            leaving = _junction(edge, edges[index + 1]) if index + 1 < len(edges) else None
            if (index and entered is None) or (index + 1 < len(edges) and leaving is None):
                return
            source = motion.lane_position if index == 0 else 0 if entered == begin else length
            target = destination['position'] if index == len(edges) - 1 else 0 if leaving == begin else length
        distance = abs(target - source)
        if distance > 1e-8:
            finish = time + distance / speed
            if time < runtime.timeline_end_seconds:
                yield route[index], ('forward' if target > source else 'reverse'), time, min(finish, runtime.timeline_end_seconds)
            time = finish


def summarize_paths(runtime, flows):
    by_flow = defaultdict(Counter)
    occupied = defaultdict(Counter)
    directions = defaultdict(lambda: defaultdict(Counter))
    windows = defaultdict(dict)
    for pid, motion in runtime.current.persons.items():
        person = flows.people[pid]
        flow = person['flow_id']
        occupied[flow][motion.edge_id] += 1
        if pid in flows.dwelling or motion.edge_id.startswith(':'):
            continue
        stage = tuple(runtime.adapter.current_person_stage(pid).edges)
        if stage.count(motion.edge_id) != 1:
            continue
        route = stage[stage.index(motion.edge_id):]
        if not route or route[-1] != person['destination']['edge']:
            continue
        by_flow[flow][route] += 1
        for edge, direction, start, end in _passages(runtime, motion, route, person['destination'], flows.speeds[pid]):
            if edge == motion.edge_id:
                directions[flow][edge][direction] += 1
            key = flow, direction
            record = windows[edge].setdefault(key, [start, end, 0])
            record[0] = min(record[0], start)
            record[1] = max(record[1], end)
            record[2] += 1

    overlaps = []
    for edge, entries in windows.items():
        rows = list(entries.items())
        for index, ((left, left_dir), a) in enumerate(rows):
            for (right, right_dir), b in rows[index + 1:]:
                start, end = max(a[0], b[0]), min(a[1], b[1])
                if left == right or end <= start:
                    continue
                overlaps.append({'edgeId': edge, 'flowIds': [left, right],
                    'directions': [left_dir, right_dir], 'estimatedOverlapSeconds': [round(start, 1), round(end, 1)],
                    'routeMembers': [a[2], b[2]]})
    overlaps.sort(key=lambda item: (item['directions'][0] == item['directions'][1], -min(item['routeMembers']), item['estimatedOverlapSeconds'][0]))
    summaries = []
    for flow in flows.metrics()['flows']:
        flow_id = flow['id']
        current_edges = dict(occupied[flow_id].most_common(10))
        summaries.append({**flow, 'currentEdges': current_edges,
            'currentRouteDirections': {edge: dict(directions[flow_id][edge]) for edge in current_edges if edge in directions[flow_id]},
            'mainRemainingRoutes': [{'edgeIds': list(route), 'count': count} for route, count in by_flow[flow_id].most_common(3)]})
    return {'flowPaths': summaries, 'predictedOverlaps': overlaps[:18],
            'predictionMethod': '当前步行路线与设定速度的自由通行时间估算，未模拟拥挤、排队和干预。仅列出可定位的共用路段时间窗口。'}
