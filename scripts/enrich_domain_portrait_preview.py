"""Attach reviewed public evidence to an explicitly selected preview task only."""
import argparse
import json
from pathlib import Path
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', required=True)
    parser.add_argument('--title', help='Optional title for this independent preview task')
    parser.add_argument('--base', default='http://127.0.0.1:8768/api/crowdSim')
    args = parser.parse_args()
    if args.task in {'4d9b7e94c91d4eb7a1d52cd722ba5c43', '6f3fb29cda9646bd9599aceeab3d72fc'}:
        raise SystemExit('Protected original task: no writes allowed.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def api(path, payload=None):
        req = urllib.request.Request(args.base + path, data=None if payload is None else json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'}, method='GET' if payload is None else 'PUT')
        with opener.open(req, timeout=120) as response:
            return json.load(response)['data']

    task = api('/analysis-tasks/' + args.task)
    portrait = task.get('components', {}).get('domain_portrait', {})
    if portrait.get('mode') != 'preview':
        raise SystemExit('Only an existing independent preview can be enriched.')
    evidence = json.loads((Path(__file__).resolve().parent.parent / 'data/domain_portrait/monument_evidence_preview.json').read_text())
    existing = {row['id']: row for row in portrait['evidence']}
    existing.update({row['id']: row for row in evidence['evidence']})
    portrait['evidence'] = list(existing.values())
    candidates = {row['id']: row for row in portrait.get('candidates', [])}
    for candidate in evidence['candidates']:
        retained = candidates.get(candidate['id'], {}).get('retained', candidate.get('retained', False))
        candidates[candidate['id']] = {**candidate, 'retained': retained}
    portrait['candidates'] = list(candidates.values())
    for dimension in portrait['dimensions']:
        update = evidence['dimension_updates'].get(dimension['id'], {})
        dimension.update(update)
    portrait['overview'] = evidence['overview']
    portrait['evidence_retrieved_at'] = evidence['retrieved_at']
    if 'visualizations' in evidence:
        # Resolve points against the pinned task network, never a different/latest environment.
        network = api('/analysis-tasks/' + args.task + '/road-network')
        binding = task.get('components', {}).get('road_network_binding', {})
        if binding.get('environment_id') != 'shanghai-monument-portrait-region' or binding.get('environment_revision') != 6:
            raise SystemExit('The visual evidence anchors require the monument environment revision 6.')
        features = {f['properties']['sumo_edge_id']: f for f in network['roads']['features']}
        visualizations = evidence['visualizations']
        for point in visualizations['spatial_points']:
            anchor = point['anchor']
            coordinates = features[anchor['edge_id']]['geometry']['coordinates']
            if anchor['method'] == 'eastmost':
                point['coordinates'] = max(coordinates, key=lambda p: p[0])
            else:
                # Short connector segments: a representative midpoint, not a surveyed gate.
                point['coordinates'] = [(coordinates[0][i] + coordinates[-1][i]) / 2 for i in (0, 1)]
            point['coordinate_source'] = {**binding, 'edge_id': anchor['edge_id'], 'crs': 'WGS84'}
        portrait['visualizations'] = visualizations
    if args.title:
        fields = ('title', 'summary', 'event_id', 'event_label', 'time_window', 'template_case', 'status', 'region', 'population', 'references', 'components')
        payload = {key: task[key] for key in fields if key in task}
        payload['title'] = args.title
        payload['components']['domain_portrait'] = portrait
        saved = api('/analysis-tasks/' + args.task, {**payload, 'revision': task['revision']})
    else:
        saved = api('/analysis-tasks/' + args.task + '/components/domain_portrait', {'revision': task['revision'], 'data': portrait})
    print(json.dumps({'task_id': saved['id'], 'revision': saved['revision'], 'evidence': len(portrait['evidence']), 'candidates': len(portrait['candidates'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
