import asyncio
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import websockets

from crowdsim.core.population_manager import PopulationManager
from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder
from crowdsim.infrastructure.websocket_server import OverlayServer
from crowdsim.scenarios.generated_hotspot_demand import HotspotDemandSpec
from tests.test_websocket_contract import CapturingClient, has_sumo

ROOT = Path(__file__).resolve().parents[1]
BUND = ROOT / 'scenarios' / 'shanghai_bund'


def specification():
    return HotspotDemandSpec(BUND / 'bund_ped.rou.xml', ROOT / 'config' / 'crowd_hotspots.json')


class GeneratedHotspotDemandTests(unittest.TestCase):
    def test_bounds_and_default(self):
        spec = specification()
        self.assertEqual(700, spec.capabilities()['default_count'])
        for valid in [0, 1, 700, 10000]:
            spec.validate_count(valid)
        for invalid in [-1, 10001, 1.5, True, '700', None]:
            with self.assertRaises(ValueError):
                spec.validate_count(invalid)

    def test_exact_counts_distribution_and_run_isolation(self):
        spec = specification()
        original = (BUND / 'bund_hotspot.rou.xml').read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            for count in [0, 1, 12, None, 1000]:
                expected = 700 if count is None else count
                with self.subTest(count=count):
                    output, report = spec.generate(Path(directory) / str(count), BUND / 'bund.net.xml', count)
                    people = ET.parse(output).getroot().findall('person')
                    self.assertEqual(expected, len(people))
                    self.assertEqual(expected, len({p.get('id') for p in people}))
                    self.assertEqual(0, report['background_count'])
                    self.assertEqual(expected, report['effective_count'])
                    self.assertEqual(expected, sum(report['spawn_edge_counts'].values()))
                    self.assertEqual(expected, sum(report['viewing_zone_counts'].values()))
                    self.assertTrue(all(p.get('id').startswith('hotspot.') for p in people))
                    self.assertTrue(all(len(p.findall('walk')) == 2 and not p.findall('stop') for p in people))
                    if expected >= 700:
                        self.assertGreater(len(report['spawn_edge_counts']), 1)
                        self.assertGreater(len({p.get('depart') for p in people}), 100)
                        offset = report['timeline_shift_seconds']
                        self.assertEqual([600.0 - offset, 800.0 - offset], report['activity_window_seconds'])
                    if expected:
                        self.assertEqual('aligned', report['timeline_alignment_status'])
                        self.assertEqual(0.0, min(float(p.get('depart')) for p in people))
                    else:
                        self.assertEqual('no_visitors', report['timeline_alignment_status'])
                        self.assertEqual(0.0, report['timeline_shift_seconds'])
                    archived = json.loads((output.parent / 'demand_generation.json').read_text())
                    self.assertEqual(count, archived['requested_count'])
                    self.assertEqual(report['timeline_shift_seconds'], archived['timeline_shift_seconds'])
                    self.assertEqual(report['activity_window_seconds'], archived['activity_window_seconds'])
                    self.assertTrue((output.parent / 'hotspot.config.json').is_file())
                    manager = PopulationManager([output])
                    self.assertEqual(expected, manager.diagnostics()['planned'])
            repeat, _ = spec.generate(Path(directory) / 'repeat', BUND / 'bund.net.xml', 12)
            self.assertEqual((Path(directory) / '12' / repeat.name).read_bytes(), repeat.read_bytes())
        self.assertEqual(original, (BUND / 'bund_hotspot.rou.xml').read_bytes())


@unittest.skipUnless(has_sumo(), 'real SUMO binary is required')
class GeneratedHotspotRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_configuration_reset_and_real_sumo_demand(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / 'config', root / 'config')
            with patch('crowdsim.core.simulation_runtime.PROJECT_ROOT', root), patch(
                'crowdsim.core.simulation_runtime.ExperimentRecorder',
                side_effect=lambda *args: ExperimentRecorder(*args, root=root / 'runs'),
            ):
                runtime = SimulationRuntime(
                    BUND / 'bund.hotspot.sumocfg', pedestrian_route_files=[BUND / 'bund_hotspot.rou.xml'],
                    demand_mode='generated_hotspot', hotspot_demand_spec=specification(), scenario_name='hotspot',
                    extra_sumo_args=['--route-steps', '0'],
                )
                server = OverlayServer(runtime, '127.0.0.1', 0)
                client = CapturingClient(); server.client = client
                async def command(action='configure', **payload):
                    await server._handle_message(json.dumps({'action': action, **payload}))
                    return client.messages[-1]
                try:
                    for count in [-1, 10001, True, 2.5, '12']:
                        self.assertEqual('invalid_count', (await command(count=count))['code'])
                    self.assertEqual('CREATED', runtime.state.value)
                    init = await command(count=0, request_id='zero')
                    self.assertEqual('init', init['type'], init)
                    self.assertEqual(0, init['demand']['planned'])
                    self.assertTrue(init['demand']['count_configurable'])
                    self.assertEqual(0, runtime.adapter.connection.simulation.getMinExpectedNumber())
                    empty_run = runtime.run_id
                    init = await command('reset', count=12, request_id='twelve')
                    self.assertEqual('init', init['type'], init)
                    self.assertNotEqual(empty_run, runtime.run_id)
                    self.assertEqual(12, init['demand']['planned'])
                    self.assertEqual(12, runtime.adapter.connection.simulation.getMinExpectedNumber())
                    generated = runtime.run_id
                    self.assertEqual('init', (await command(count=12))['type'])
                    self.assertEqual(generated, runtime.run_id)
                    self.assertEqual('count_requires_reset', (await command(count=20))['code'])
                    self.assertEqual('duplicate_request', (await command(request_id='twelve'))['code'])
                    manifest = json.loads((root / 'runs' / generated / 'manifest.json').read_text())
                    self.assertEqual(12, manifest['demand']['effective_count'])
                    self.assertEqual(12, manifest['demand_generation']['effective_count'])
                    report = manifest['demand_generation']
                    hotspot = next(item for item in init['hotspots'] if item['id'] == report['hotspot_id'])
                    self.assertEqual(report['activity_window_seconds'], hotspot['activity_window_seconds'])
                    self.assertEqual(report['visitor_arrival_profile'], hotspot['visitor_arrival_profile'])
                    self.assertEqual(report['timeline_shift_seconds'], hotspot['timeline_shift_seconds'])
                    runtime.start()
                    for _ in range(4):
                        await runtime.tick_async()
                    self.assertTrue(runtime.population.ledger.departed_ids)
                    self.assertLessEqual(runtime.current.time_seconds, 2.0)
                    self.assertEqual(12, runtime.frame()['metrics']['population']['planned'])
                    runtime.close()
                    init = await command(request_id='default')
                    self.assertEqual('init', init['type'], init)
                    self.assertEqual(700, init['demand']['planned'])
                    self.assertIsNone(init['demand']['requested_count'])
                    runtime.close()
                    server.client = None
                    async with websockets.serve(server._handler, '127.0.0.1', 0) as listener:
                        port = listener.sockets[0].getsockname()[1]
                        async with websockets.connect(f'ws://127.0.0.1:{port}') as socket:
                            await socket.send(json.dumps({'action': 'configure', 'count': 3, 'request_id': 'wire'}))
                            preparing = json.loads(await asyncio.wait_for(socket.recv(), 10))
                            self.assertEqual('preparing', preparing['type'])
                            self.assertEqual('wire', preparing['request_id'])
                            pong = await socket.ping()
                            await asyncio.wait_for(pong, 5)
                            initialized = json.loads(await asyncio.wait_for(socket.recv(), 10))
                            self.assertEqual('init', initialized['type'])
                            self.assertEqual(3, initialized['demand']['planned'])
                            self.assertEqual('wire', initialized['request_id'])
                finally:
                    await server._stop_runtime()
