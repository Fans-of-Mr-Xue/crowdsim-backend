"""Automated class-two integration using local SUMO; no frontend/API services."""
from collections import Counter
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from crowdsim.scenarios.class2_crowding import prepare_requirement, validate_binding
from crowdsim.scenarios.class2_forecast import BaselineCollector
from crowdsim.scenarios.class2_society import publish_class2_society
from crowdsim.infrastructure.requirement_repository import RequirementRepository
from dialog_storage.during_requirements import DuringRequirementRepository
from crowdsim_overlay_server import build_requirement_runtime, parse_args
from tests.class2_flow_fixtures import flow_request
from tests.test_during_workflow import confirmed
import crowdsim.core.simulation_runtime as runtime_module


def request():
    body = flow_request(2, durationSeconds=600)
    body.update(count=15, backgroundCount=13)
    return body


@pytest.mark.parametrize('value', [-1, True, 1.5, 15, 10001])
def test_background_count_invalid(value):
    with pytest.raises(ValueError):
        prepare_requirement({**request(), 'backgroundCount': value})


def test_background_binding_is_frozen():
    payload = prepare_requirement(request()).payload
    assert validate_binding(payload)['background_count'] == 13
    changed = deepcopy(payload)
    changed['class2_demo']['background_count'] = 12
    with pytest.raises(ValueError):
        validate_binding(changed)


def test_real_sumo_background_identity_and_five_second_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv('CROWDSIM_RUNS_ROOT', str(tmp_path/'runs'))
    monkeypatch.setenv('CROWDSIM_EXPERIMENT_DIR', str(tmp_path/'control'))
    body = request()
    record = RequirementRepository(tmp_path/'requirements').create(prepare_requirement(body), purpose='experiment')
    binding = record['requirement']['class2_demo']
    # Use the real confirmed snapshot repository/publication boundary.
    state = confirmed(2)
    r = state['requirement']['draft']['requirement']
    r.update(population=deepcopy(record['requirement']['population']), experiment={'duration_seconds': body['durationSeconds']},
             observation={'metric_ids': body['metricIds']}, class2={'mode': 'automated_context',
             'flow_design': body['flowDesign'], 'background_population': {'count': body['backgroundCount']}})
    state['requirement']['draft']['savedSnapshotId'] = body['source']['snapshotId']
    state['requirement']['draft']['confirmation']['id'] = body['source']['snapshotId']
    state['requirement']['snapshots'][0].update(id=body['source']['snapshotId'], requirement=deepcopy(r))
    repository = DuringRequirementRepository(tmp_path/'conversation')
    repository.save(body['source']['conversationId'], state=state, expected_revision=0, mutation_id='confirm')
    model = publish_class2_society({**record, 'class2_demo': binding}, requirements=repository)
    assert model['artificial_society']['background']['dwellSeconds'] == 5
    runtime = build_requirement_runtime(parse_args(['--scenario', 'hotspot']), record)
    try:
        with patch.object(runtime_module, 'PROJECT_ROOT', tmp_path):
            runtime.initialize()
        controller = runtime.class2_flows
        ids = set(runtime.current.persons)
        assert runtime.state.value == 'READY'
        assert len(ids) == 15 and len(controller.background_ids) == 13
        assert all(p.speed == 0 for p in runtime.current.persons.values())
        assert ids == set(runtime.population.profiles)
        profiles = deepcopy(runtime.population.profiles)
        # Each requested marginal distribution is apportioned over the background
        # cohort itself, independently of the separately configured activity flow.
        assignments = runtime.population.profile_sampler._assignments
        for dimension, rows in body['populationDistributions'].items():
            raw = [13 * row['percent'] / 100 for row in rows]
            expected = [int(n) for n in raw]
            for i in sorted(range(len(rows)), key=lambda i: (-(raw[i]-expected[i]), i))[:13-sum(expected)]:
                expected[i] += 1
            actual = Counter(assignments[pid][dimension] for pid in controller.background_ids)
            for row, count in zip(rows, expected):
                key = row['label'] if dimension == 'origin' else row.get('code') or row.get('id') or row['label']
                assert actual[key] == count
        runtime.init_frame()
        identity_fields = ('age_band', 'gender', 'origin', 'crowd_role')
        def displayed_identities():
            return {p['id']: {key: p['state'][key] for key in identity_fields}
                    for p in runtime.serializer.build_frame(runtime)['pedestrians']}
        identities = displayed_identities()
        assert set(identities) == ids
        for pid in ids:
            assert identities[pid] == {key: getattr(profiles[pid], key) for key in identity_fields}
        controller.control.context(runtime)
        baseline = BaselineCollector(runtime)
        baseline.sample(runtime)
        starts, cycles = {}, Counter()
        destinations = {pid: deepcopy(p['destination']) for pid, p in controller.people.items()}
        runtime.start()
        while runtime.state.value == 'RUNNING':
            before = runtime.time_seconds
            runtime.tick()
            assert set(runtime.current.persons) == ids
            for pid in controller.background_ids:
                if pid in controller.dwelling:
                    if pid in starts:
                        assert runtime.current.persons[pid].speed == 0
                    starts.setdefault(pid, runtime.time_seconds)
                if controller.people[pid]['destination'] != destinations[pid]:
                    assert pid in starts
                    assert before - starts.pop(pid) == pytest.approx(5)
                    assert pid not in controller.dwelling
                    cycles[pid] += 1
                    destinations[pid] = deepcopy(controller.people[pid]['destination'])
            baseline.sample(runtime)
        assert sum(cycles.values()) >= 2
        assert max(cycles.values()) >= 2
        assert ids-controller.background_ids <= controller.dwelling
        assert runtime.population.profiles == profiles
        assert displayed_identities() == identities
        assert runtime.population.diagnostics()['conservation_error'] == 0
        assert baseline.result(runtime)['populationConserved']
        # Normal person frames still expose the existing profile-backed identity.
        assert len(runtime.current.persons) == 15
        print(json.dumps({'background': 13, 'activity': 2, 'cycles': dict(cycles),
                          'duration': runtime.time_seconds}, ensure_ascii=False))
    finally:
        runtime.close()


def test_full_size_background_reuses_routes_without_mutating_frozen_positions():
    from crowdsim.scenarios.class2_crowding import (
        _background_positions, background_layout, ROOT, load_preset,
    )
    body = flow_request(20)
    body.update(count=333, backgroundCount=313)
    _background_positions.cache_clear()
    spec = prepare_requirement(body)
    first = _background_positions.cache_info()
    assert first.misses == 1
    # Validating an already prepared requirement must not run another set of
    # hundreds of shortest-path queries (the browser request used to time out).
    with patch('crowdsim.decision.position_aware_router.PositionAwarePedestrianRouter.route',
               side_effect=AssertionError('background routes were recalculated')):
        assert validate_binding(spec.payload)['background_count'] == 313
        preset = load_preset()[0]
        seed = spec.payload['class2_demo']['seed']
        layout = background_layout({'positions': []}, 313, seed, ROOT/preset['network'])
        original = deepcopy(layout['positions'][0])
        layout['positions'][0]['destination']['position'] = -1
        restored = background_layout({'positions': []}, 313, seed, ROOT/preset['network'])
        assert restored['positions'][0] == original
        assert len(restored['positions']) == 313
    assert _background_positions.cache_info().misses == 1


def test_random_draws_missing_small_reachable_walkway_do_not_stop_the_run():
    import random
    from types import SimpleNamespace
    from crowdsim.scenarios.class2_background import next_trip
    from crowdsim.decision.position_aware_router import PositionRouteUnavailable
    source = {'edge': '178564467', 'position': 2.114121742518687}
    def route(start, target):
        if start.edge_id != target.edge_id:
            raise PositionRouteUnavailable('disconnected')
        return SimpleNamespace(edges=(start.edge_id,), distance_m=abs(start.offset_m-target.offset_m))
    router = SimpleNamespace(route=route)
    # Force all 200 random draws to miss the isolated edge encountered by
    # test18 at T+11s. A reachable on-edge destination must still be found.
    with patch('crowdsim.scenarios.class2_background.random_point',
               return_value={'edge': 'unreachable', 'position': 1}):
        target, trip = next_trip(router, random.Random(42), source)
    assert target['edge'] == source['edge']
    assert trip.distance_m > .1


def test_runtime_destinations_stay_reachable_and_reuse_prepared_trees():
    import random
    from crowdsim.infrastructure.network_adapter import ResearchNetwork
    from crowdsim.decision.position_aware_router import PositionAwarePedestrianRouter
    from crowdsim.scenarios.class2_background import BackgroundTripPlanner
    from crowdsim.scenarios.class2_flow_layout import position
    from crowdsim.scenarios.class2_crowding import ROOT, load_preset
    network = ResearchNetwork(str(ROOT/load_preset()[0]['network']))
    router = PositionAwarePedestrianRouter(network)
    planner = BackgroundTripPlanner(router)
    prepared = router.diagnostics()['tree_builds']
    rng = random.Random(42)
    calls = router.diagnostics()['route_queries']
    for lane in planner.lanes:
        source = position(lane, lane['length']/2)
        for _ in range(20):
            target, route = planner.next_trip(rng, source)
            assert route.edges[0] == source['edge']
            assert route.edges[-1] == target['edge']
            assert route.distance_m > .06
            assert planner.components[(source['edge'], 0)] == planner.components[(target['edge'], 0)]
            source = target
    assert router.diagnostics()['tree_builds'] == prepared
    assert router.diagnostics()['route_queries'] - calls == 20*len(planner.lanes)
