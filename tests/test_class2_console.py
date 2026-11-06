"""Pure class-two console contracts; no SUMO processes, sockets or models."""
from copy import deepcopy
from types import SimpleNamespace

from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.scenarios.class2_crowding import observation_map_geometry, prepare_requirement as prepare_flow_requirement

from tests.class2_flow_fixtures import flow_request


def prepare_requirement(body):
    return prepare_flow_requirement(flow_request(**body))


def test_console_collects_all_five_macro_indicators():
    record = prepare_requirement({'problemType': 2, 'count': 100,
                                  'phenomenon': '平台已出现聚集', 'durationSeconds': 300})
    assert record.payload['observation']['metric_ids'] == [
        'global-density', 'local-density', 'global-speed', 'local-speed',
        'boundary-density-difference']


def test_map_geometry_preserves_projected_observations_and_adds_geographic_coordinates():
    geometry = {'cells': [{'cell_id': 'a', 'geometry': {'type': 'Polygon',
                'coordinates': [[[0, 0], [10, 0], [0, 10], [0, 0]]]}}],
                'boundaries': [{'boundary_id': 'a:east', 'geometry': {
                    'type': 'LineString', 'coordinates': [[0, 0], [0, 10]]}}]}
    frozen = deepcopy(geometry)
    network = SimpleNamespace(xy_to_lonlat=lambda x, y: (121 + x / 100000, 31 + y / 100000))
    result = observation_map_geometry(network, geometry)
    assert result['map_crs'] == 'EPSG:4326'
    assert result['cells'][0]['map_geometry']['coordinates'][0][1] == [121.0001, 31]
    assert result['boundaries'][0]['map_geometry']['coordinates'][1] == [121, 31.0001]
    assert result['cells'][0]['geometry'] == frozen['cells'][0]['geometry']
    assert geometry == frozen


def test_first_class_init_keeps_its_original_geometry_and_payload():
    original = {'type': 'init', 'observation_geometry': {'cells': [], 'boundaries': []}}
    runtime = SimpleNamespace(
        serializer=SimpleNamespace(build_init=lambda _: deepcopy(original)),
        generated_demand_spec=SimpleNamespace(is_class2_demo=False),
        artificial_society_diagnostics=lambda: {'enabled': True},
        performance=SimpleNamespace(metadata=lambda: {'enabled': False}))
    assert SimulationRuntime.init_frame(runtime) == {
        **original, 'artificial_society': {'enabled': True},
        'performance_measurement': {'enabled': False}}
    runtime.generated_demand_spec.is_class2_demo = True
    runtime.network = SimpleNamespace(xy_to_lonlat=lambda x, y: (x, y))
    assert SimulationRuntime.init_frame(runtime)['observation_geometry']['map_crs'] == 'EPSG:4326'


def test_identical_inputs_from_different_conversations_or_snapshots_get_distinct_requirements(tmp_path):
    from crowdsim.infrastructure.requirement_repository import RequirementRepository
    from crowdsim.scenarios.class2_crowding import validate_binding
    repository = RequirementRepository(tmp_path)
    ids = []
    for conversation, snapshot in [('a', 'one'), ('b', 'one'), ('a', 'two')]:
        spec = prepare_requirement({'problemType': 2, 'count': 100, 'phenomenon': '平台聚集',
                                    'source': {'conversationId': conversation, 'snapshotId': snapshot}})
        assert validate_binding(spec.payload)['source'] == {'conversationId': conversation, 'snapshotId': snapshot}
        ids.append(repository.create(spec, purpose='experiment', reuse_identical=True)['requirement_id'])
    assert len(set(ids)) == 3


def test_execution_source_rejects_incomplete_or_foreign_fields():
    import pytest
    for source in [None, {}, {'conversationId': 'a'}, {'conversationId': '../a', 'snapshotId': 'one'},
                   {'conversationId': 'a', 'snapshotId': 'one', 'model': 'old'}]:
        with pytest.raises(ValueError, match='source'):
            prepare_requirement({'problemType': 2, 'count': 100, 'phenomenon': '聚集', 'source': source})


def initial_visual_runtime(*, class_two=True, count=7):
    """A frozen in-memory motion snapshot, with no simulator or recorder."""
    from unittest.mock import Mock
    from crowdsim.domain.crowd_visual_state import CrowdVisualPolicy
    from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot
    from crowdsim.environment.crowd_environment import CrowdEnvironment
    from crowdsim.infrastructure.sumo_adapter import SumoStepResult
    motions = {
        f'person-{i}': MotionSnapshot(f'person-{i}', .5, i * .001, 0, 121, 31,
                                     0, 'walkway', 'walkway_0', i * .001, 0, 0, 2)
        for i in range(count)
    }
    if count:
        motions['outside-cluster'] = MotionSnapshot('outside-cluster', .5, 40, 0, 121, 31,
                                                    0, 'walkway', 'walkway_0', 40, 0, 0, 2)
    lane = SimpleNamespace(getWidth=lambda: 2, allows=lambda kind: kind == 'pedestrian')
    network = SimpleNamespace(edges={'walkway': SimpleNamespace(getLanes=lambda: [lane])})
    runtime = SimulationRuntime.__new__(SimulationRuntime)
    runtime.run_id = 'class2-initial-visual'
    runtime.snapshot_index = 0
    runtime.current = SumoStepResult(.5, motions, {}, tuple(motions), ())
    runtime.population = SimpleNamespace(
        profiles={key: AgentProfile(key, perception_radius=1) for key in motions},
        states={key: AgentState(key) for key in motions})
    runtime.generated_demand_spec = SimpleNamespace(is_class2_demo=class_two)
    runtime.environment = CrowdEnvironment(network)
    runtime.hazards = SimpleNamespace(zones={})
    runtime.visual_policy = CrowdVisualPolicy()
    runtime.observations = {}
    runtime.latest_metrics = {}
    runtime.adapter = SimpleNamespace(step=Mock(side_effect=AssertionError('no engine step permitted')))
    runtime.state = 'READY'
    return runtime


def test_class_two_ready_computes_density_and_colors_without_starting_or_moving_people():
    from dataclasses import asdict
    runtime = initial_visual_runtime()
    motions = dict(runtime.current.persons)
    before = {key: asdict(state) for key, state in runtime.population.states.items()}
    runtime._refresh_initial_visual_states()
    assert len(runtime.observations) == 8
    assert runtime.visual_states['person-0']['density_person_per_m2'] == 1.75
    assert runtime.visual_states['person-0']['visual_state'] == 'crowded'
    assert runtime.visual_states['person-0']['color'] == 'orange'
    assert runtime.visual_states['outside-cluster']['visual_state'] == 'normal'
    assert runtime.latest_metrics['visual_state_counts']['crowded'] == 7
    assert runtime.latest_metrics['visual_density_unknown_count'] == 0
    assert runtime.time_seconds == .5 and runtime.snapshot_index == 0
    assert runtime.state == 'READY' and runtime.current.persons == motions
    assert {key: asdict(state) for key, state in runtime.population.states.items()} == before
    runtime.adapter.step.assert_not_called()
    # Repeated reads/reclassification must not accumulate five-second evidence.
    runtime._refresh_initial_visual_states()
    assert runtime.visual_states['person-0']['critical_density_duration_seconds'] == 0


def test_class_two_initial_high_density_does_not_invent_sustained_red_history():
    runtime = initial_visual_runtime(count=20)
    runtime._refresh_initial_visual_states()
    assert runtime.visual_states['person-0']['density_person_per_m2'] == 5
    assert runtime.visual_states['person-0']['visual_state'] == 'crowded'
    assert runtime.latest_metrics['visual_state_counts']['high_risk'] == 0
    assert runtime.population.states['person-0'].critical_density_duration == 0
    assert runtime.population.states['person-0'].dense_low_speed_duration == 0


def test_class_one_initial_visual_calculation_keeps_its_existing_behavior():
    from unittest.mock import Mock
    runtime = initial_visual_runtime(class_two=False)
    runtime.environment.observe = Mock(side_effect=AssertionError('class-one initialization is unchanged'))
    runtime._refresh_initial_visual_states()
    runtime.environment.observe.assert_not_called()
    assert runtime.observations == {}
    assert runtime.latest_metrics['visual_state_counts']['normal'] == 8
    assert runtime.latest_metrics['visual_density_unknown_count'] == 8


def test_empty_class_two_initialization_does_not_keep_stale_observations():
    runtime = initial_visual_runtime(count=0)
    runtime.observations = {'stale': object()}
    runtime._refresh_initial_visual_states()
    assert runtime.observations == {} and runtime.visual_states == {}
    assert runtime.latest_metrics['visual_density_unknown_count'] == 0
    runtime.adapter.step.assert_not_called()


def test_initial_population_materialization_uses_one_preparation_step_and_keeps_real_time():
    from unittest.mock import Mock
    from crowdsim.infrastructure.sumo_adapter import SumoStepResult
    populated = initial_visual_runtime(count=1).current
    empty = SumoStepResult(0, {}, {}, (), ())
    runtime = SimpleNamespace(
        demand_generation_report={'initial_population': True},
        population=SimpleNamespace(ledger=SimpleNamespace(planned_ids=set(populated.persons))),
        adapter=SimpleNamespace(step=Mock(return_value=populated)))
    ready = SimulationRuntime._materialize_initial_population(runtime, empty)
    assert ready is populated and ready.time_seconds == .5
    runtime.adapter.step.assert_called_once_with()
    # No additional warm-up is needed if all people were already materialized.
    assert SimulationRuntime._materialize_initial_population(runtime, ready) is ready
    runtime.adapter.step.assert_called_once_with()


def test_class_two_preparation_is_idempotent_without_opening_a_server_or_initializing_sumo(tmp_path):
    import asyncio
    import json
    from unittest.mock import AsyncMock, Mock
    from crowdsim.during_server import DuringOverlayServer
    from crowdsim.infrastructure.requirement_repository import RequirementRepository
    simulator = SimpleNamespace(initialize=Mock(side_effect=AssertionError('no simulator allowed')))
    server = DuringOverlayServer(simulator, '127.0.0.1', 0, requirement_repository=RequirementRepository(tmp_path))
    owner = object()
    server.client = owner
    payload = flow_request(**{'problemType': 2, 'count': 700, 'phenomenon': '现场已经聚集',
               'source': {'conversationId': 'study', 'snapshotId': 'snapshot-one'}})

    async def prepare_twice():
        request = SimpleNamespace(json=AsyncMock(return_value=payload))
        first = await server._class2_requirement_http(request)
        second = await server._class2_requirement_http(request)
        assert first.status == second.status == 201
        return json.loads(first.text), json.loads(second.text)

    first, second = asyncio.run(prepare_twice())
    assert first['requirement_id'] == second['requirement_id']
    assert first['fingerprint'] == second['fingerprint']
    assert len(list(tmp_path.glob('req-*.json'))) == 1
    assert server.client is owner
    simulator.initialize.assert_not_called()


def test_idempotent_class_two_receipt_survives_repository_reopen_and_resource_changes_get_a_new_id(tmp_path):
    from crowdsim.infrastructure.requirement_repository import RequirementRepository
    from crowdsim.domain.requirement_spec import RequirementSpec
    spec = prepare_requirement({'problemType': 2, 'count': 700, 'phenomenon': '聚集'})
    repository = RequirementRepository(tmp_path)
    first = repository.create(spec, purpose='experiment', reuse_identical=True)
    reopened = RequirementRepository(tmp_path).create(spec, purpose='experiment', reuse_identical=True)
    assert first == reopened
    changed = deepcopy(spec.payload)
    changed['class2_demo']['fingerprint'] = 'sha256:' + 'f' * 64
    newer = repository.create(RequirementSpec.parse(changed), purpose='experiment', reuse_identical=True)
    assert newer['requirement_id'] != first['requirement_id']
    assert repository.load(first['requirement_id']) == first


def test_concurrent_identical_class_two_preparations_publish_one_receipt(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from crowdsim.infrastructure.requirement_repository import RequirementRepository
    spec = prepare_requirement({'problemType': 2, 'count': 300, 'phenomenon': '聚集'})
    repository = RequirementRepository(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(lambda _: repository.create(spec, purpose='experiment', reuse_identical=True), range(12)))
    assert len({row['requirement_id'] for row in records}) == 1
    assert len(list(tmp_path.glob('req-*.json'))) == 1
    assert not list(tmp_path.glob('*.tmp'))


def test_default_requirement_storage_behavior_is_unchanged_for_other_callers(tmp_path):
    from crowdsim.infrastructure.requirement_repository import RequirementRepository
    from crowdsim.domain.requirement_spec import RequirementSpec
    from tests.test_requirement_submission import requirement_payload
    from tests.test_requirement_location_v2 import located_requirement
    repository = RequirementRepository(tmp_path)
    legacy = RequirementSpec.parse(requirement_payload())
    first = repository.create(legacy)
    second = repository.create(legacy)
    assert first['fingerprint'] == second['fingerprint']
    assert first['requirement_id'] != second['requirement_id']
    current = RequirementSpec.parse(located_requirement(memorial=True))
    assert repository.create(current)['requirement_id'] == repository.create(current)['requirement_id']


def test_map_geometry_converts_nested_and_empty_geometry_collections_without_mutating_source():
    geometry = {'cells': [], 'boundaries': [{'boundary_id': 'clipped-scope',
        'geometry': {'type': 'GeometryCollection', 'geometries': [
            {'type': 'LineString', 'coordinates': [[0, 0], [10, 0]]},
            {'type': 'Point', 'coordinates': [3, 4]},
            {'type': 'GeometryCollection', 'geometries': [
                {'type': 'MultiLineString', 'coordinates': [[[1, 2], [3, 4]], [[5, 6], [7, 8]]]},
                {'type': 'GeometryCollection', 'geometries': []},
            ]},
        ]}}]}
    frozen = deepcopy(geometry)
    network = SimpleNamespace(xy_to_lonlat=lambda x, y: (121 + x / 100000, 31 + y / 100000))
    result = observation_map_geometry(network, geometry)
    mapped = result['boundaries'][0]['map_geometry']
    assert mapped['type'] == 'GeometryCollection' and 'coordinates' not in mapped
    assert mapped['geometries'][0]['coordinates'][1] == [121.0001, 31]
    assert mapped['geometries'][1]['coordinates'] == [121.00003, 31.00004]
    nested = mapped['geometries'][2]['geometries']
    assert nested[0]['coordinates'][1][1] == [121.00007, 31.00008]
    assert nested[1] == {'type': 'GeometryCollection', 'geometries': []}
    assert geometry == frozen


def test_real_memorial_observation_geometry_serializes_through_class_two_init_without_sumo():
    """Exercise the real clipped grid and init-send path, with no engine/listener."""
    import asyncio
    import json
    from unittest.mock import AsyncMock
    from crowdsim.infrastructure.network_adapter import ResearchNetwork
    from crowdsim.infrastructure.observation_metrics import ObservationCollector
    from crowdsim.infrastructure.websocket_server import OverlayServer
    from crowdsim.scenarios.class2_crowding import ROOT, load_preset
    preset, _, _ = load_preset()
    network = ResearchNetwork(str(ROOT / preset['network']))
    spec = prepare_requirement({'problemType': 2, 'count': 700, 'phenomenon': '现场已拥挤'})
    collector = ObservationCollector.from_requirement({'requirement': spec.payload}, network)
    geometry = collector.geometry_metadata()
    frozen = deepcopy(geometry)
    assert any(row['geometry']['type'] == 'GeometryCollection' for row in geometry['boundaries'])
    original = {'type': 'init', 'run_id': 'offline-class2', 'runtime_state': 'READY',
                'observation_geometry': geometry}
    runtime = SimpleNamespace(
        network=network, generated_demand_spec=SimpleNamespace(is_class2_demo=True),
        serializer=SimpleNamespace(build_init=lambda _: original),
        artificial_society_diagnostics=lambda: {},
        performance=SimpleNamespace(metadata=lambda: {'enabled': False}))
    runtime.init_frame = lambda: SimulationRuntime.init_frame(runtime)
    server = SimpleNamespace(simulator=runtime, _send=AsyncMock())
    asyncio.run(OverlayServer._send_init(server, 'offline-init'))
    message = server._send.call_args.args[0]
    assert message['request_id'] == 'offline-init'
    assert message['runtime_state'] == 'READY'
    result = json.loads(json.dumps(message))['observation_geometry']
    assert len(result['cells']) == len(geometry['cells'])
    assert len(result['boundaries']) == len(geometry['boundaries'])
    for source, converted in zip(geometry['boundaries'], result['boundaries']):
        assert source['boundary_id'] == converted['boundary_id']
        assert converted['geometry'] == json.loads(json.dumps(source['geometry']))
        if source['geometry']['type'] == 'GeometryCollection':
            mapped = converted['map_geometry']
            assert len(mapped['geometries']) == len(source['geometry']['geometries'])
            for part in mapped['geometries']:
                if part['type'] == 'Point':
                    lon, lat = part['coordinates']
                    assert 121 < lon < 122 and 31 < lat < 32
    assert geometry == frozen
