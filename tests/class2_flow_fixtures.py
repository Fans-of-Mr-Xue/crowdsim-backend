"""Synthetic complete class-two flow requests for focused backend checks."""
from copy import deepcopy
import json
from crowdsim.scenarios.class2_crowding import ROOT, load_preset
from crowdsim.scenarios.class2_flow_layout import flow_network


def flow_request(count=100, **changes):
    network = flow_network()
    lane = next(l for l in network['lanes'] if l['edgeId'] == '679361567#1')
    preset = load_preset()[0]
    base = json.loads((ROOT/preset['requirement_template']).read_text())['requirement']
    request = {'problemType': 2, 'count': count, 'phenomenon': '合成的用户人流研究需求',
        'requestedLocation': '人民英雄纪念塔', 'projectTitle': '第二类人流核对', 'durationSeconds': 300,
        'source': {'conversationId': 'flow-test', 'snapshotId': 'snapshot-test'},
        'mapFingerprint': network['geometrySha256'], 'populationDistributions': deepcopy(base['population']['distributions']),
        'metricIds': ['global-density', 'local-density', 'global-speed', 'local-speed', 'boundary-density-difference'],
        'flowDesign': {'version': 1, 'location_id': 'memorial-tower', 'network_sha256': network['sourceSha256'],
            'flows': [{'id': 'user-flow', 'name': '用户人流', 'count': count, 'density': 2.2, 'speed': 1.3,
                'origin': {'lane_id': lane['id'], 'offset': 25}, 'destination': {'lane_id': lane['id'], 'offset': 80}}]}}
    request.update(changes)
    return request
