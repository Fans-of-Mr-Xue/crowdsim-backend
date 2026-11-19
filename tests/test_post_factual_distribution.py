from copy import deepcopy
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock

from bson import ObjectId
from postanalysis_api.repositories.mongodb import MongoRepository
from postanalysis_api.schemas.common import ApiError
from postanalysis_api.services.distribution_targets import distribution_targets
from postanalysis_api.services.population_execution import prepare_execution_dataset
from postanalysis_api.services.execution_template import execution_readiness
from postanalysis_api.workers.memorial_scenario import compile_routes
from postanalysis_api.workers.distribution_measurement import measure_distribution
from test_post_population_execution import population_dataset


class FactualDistributionTests(unittest.TestCase):
    def test_global_only_and_old_archived_summary_never_fabricate_entry_distributions(self):
        data = {'metadata': {}, 'observations': [{'regionId': 'global', 'timeSeconds': t, 'population': 600} for t in (.5, 600)]}
        self.assertFalse(execution_readiness(data)['ready'])
        self.assertNotIn('executionProfile', prepare_execution_dataset(data)['metadata'])
        data['metadata']['sourceBinding'] = {'sourceKind': 'mongodb_archive'}
        with self.assertRaises(ApiError) as error:
            prepare_execution_dataset(data)
        self.assertEqual('FACTUAL_REIMPORT_REQUIRED', error.exception.code)

    def test_expected_coverage_rejects_whole_missing_cells_times_and_unknown_ids(self):
        for kind in ('cell', 'time', 'unknown', 'duplicate'):
            data = population_dataset()
            if kind == 'cell':
                cell = data['observations'][0]['regionId']
                data['observations'] = [row for row in data['observations'] if row['regionId'] != cell]
            elif kind == 'time':
                data['observations'] = [row for row in data['observations'] if row['timeSeconds'] != 300]
            elif kind == 'unknown':
                data['observations'][0]['regionId'] = 'unknown'
            else:
                data['observations'].append(deepcopy(data['observations'][0]))
            self.assertFalse(distribution_targets(data)['inputReady'], kind)

    def test_profiles_targets_and_positions_are_seeded_and_shared_across_worlds(self):
        data = population_dataset(); before = deepcopy(data)
        with TemporaryDirectory() as folder:
            folder = Path(folder)
            left = compile_routes(data, 42, [], 600, folder / 'left.xml')
            right = compile_routes(data, 42, [{'actionType': 'guidance', 'targetRegionId': 'C', 'atSeconds': 300,
                'parameters': {'compliance': .7, 'delaySeconds': 0}}], 600, folder / 'right.xml')
            changed_seed = compile_routes(data, 43, [], 600, folder / 'seed.xml')
        self.assertEqual(before, data)
        shared = lambda rows: [{key: row[key] for key in ('id', 'regionId', 'edgeId', 'entryPosition', 'holdingPosition', 'profile')} for row in rows]
        self.assertEqual(shared(left['cohorts']), shared(right['cohorts']))
        self.assertNotEqual(shared(left['cohorts']), shared(changed_seed['cohorts']))
        self.assertGreater(len({row['profile']['free_walking_speed'] for row in left['cohorts']}), 1)

    def test_large_targets_survive_gridfs_roundtrip_and_tampering_is_detected(self):
        repo = MongoRepository.__new__(MongoRepository)
        repo.files = MagicMock(); collection = MagicMock(); repo._collection = MagicMock(return_value=collection)
        identifier = ObjectId(); repo.files.upload_from_stream.return_value = identifier
        rows = [{'regionId': 'cell-r0000-c0000', 'timeSeconds': i / 2, 'population': 0} for i in range(96000)]
        stored = repo.create('datasets', 'u', 'w', {'observations': rows, 'observationCount': len(rows)})
        self.assertNotIn('observations', stored)
        collection.find_one.return_value = deepcopy(stored)
        call = repo.files.upload_from_stream.call_args
        encoded, metadata = call.args[1], call.kwargs['metadata']
        def stream():
            result = BytesIO(encoded); result.metadata = metadata; return result
        repo.files.open_download_stream.side_effect = lambda _: stream()
        recovered = repo.get('datasets', stored['id'], 'u', 'w')
        self.assertEqual(rows, recovered['observations'])
        with self.assertRaises(ApiError):
            repo.get('datasets', stored['id'], 'other', 'w')
        encoded += b'corrupt'
        with self.assertRaises(ApiError) as error:
            repo.get('datasets', stored['id'], 'u', 'w')
        self.assertEqual('DATASET_STORAGE_INVALID', error.exception.code)

    def test_original_clock_preserves_last_frame_zero_cells_and_mean_density(self):
        data = population_dataset(2)
        context = data['metadata']['factualContext']
        context['expectedTimes'] = [.5, 1., 1.5, 2.]
        first = data['observations'][:80]
        data['observations'] = [{**row, 'timeSeconds': time} for time in context['expectedTimes'] for row in first]
        person = context['initialDistribution'][0]
        fcd = '<fcd-export>' + ''.join(f'<timestep time="{time}"><person id="p" x="{person["x"]}" y="{person["y"]}" speed="0" angle="0"/></timestep>' for time in (0, .5, 1, 1.5)) + '</fcd-export>'
        repo = MagicMock()
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'fcd.xml'; path.write_text(fcd, encoding='utf-8')
            output = measure_distribution(path, 2, {}, repo, data)
            context['geometry']['parameters'].update(crowded_density_person_per_m2=.00001,
                                                     blocked_confirmation_seconds=.5)
            waiting = measure_distribution(path, 2, {}, repo, data,
                control_events=[{'type': 'hold', 'timeSeconds': .5, 'personId': 'p'}])
            blocked = measure_distribution(path, 2, {}, repo, data)
            self.assertEqual(0, waiting['metrics']['congestion_duration'])
            self.assertGreater(blocked['metrics']['congestion_duration'], 0)
            path.write_text(fcd.replace('<timestep time="1.5">', '<timestep time="2.5">'), encoding='utf-8')
            with self.assertRaises(ValueError):
                measure_distribution(path, 2, {}, repo, data)
        self.assertEqual([.5, 1., 1.5, 2.], [row['timeSeconds'] for row in output['timeline']])
        self.assertAlmostEqual(1 / context['geometry']['area_m2'], output['timeline'][0]['density'])
        self.assertEqual(320, output['calibration']['observationCount'])
        self.assertFalse(output['calibration']['calibrated'])
        cells = repo.append_observations.call_args_list[0].args[2]['cells']
        self.assertEqual(79, sum(row['population'] == 0 for row in cells))
        self.assertTrue(all(row['meanSpeed'] is None for row in cells if row['population'] == 0))

    def test_display_summary_remains_compatible_while_full_targets_stay_available(self):
        from postanalysis_api.services.application import PostAnalysisApp
        data = population_dataset()
        data.update(sourceKind='synthetic_reference', observationCount=len(data['observations']),
                    displayObservations=[{'timeSeconds': .5, 'regionId': 'global', 'population': 20},
                                         {'timeSeconds': 600, 'regionId': 'global', 'population': 10}])
        app = PostAnalysisApp.__new__(PostAnalysisApp)
        app.repo = MagicMock(); app.repo.get.return_value = data
        summary = app.dataset_metadata(data)
        self.assertEqual(2, summary['observationCount'])
        self.assertEqual(240, summary['spatialObservationCount'])
        self.assertNotIn('displayObservations', summary)
        self.assertEqual(2, app.timeline(data['id'], 'u', 'w', {})['total'])
        self.assertEqual(240, app.timeline(data['id'], 'u', 'w', {'view': ['spatial']})['total'])


if __name__ == '__main__':
    unittest.main()
