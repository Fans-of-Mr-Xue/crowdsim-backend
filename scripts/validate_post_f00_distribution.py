"""Read-only source audit and optional short native F00 smoke, with no DB writes."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from postanalysis_api.repositories.mongodb import MongoRepository
from postanalysis_api.services.during_archives import DuringArchiveReader
from postanalysis_api.services.factual_context import import_archive
from postanalysis_api.workers.memorial_scenario import compile_routes
from postanalysis_api.workers.distribution_demand import sumo_parameters
from postanalysis_api.workers.distribution_measurement import measure_distribution
from postanalysis_api.workers.controlled_sumo import execute


class Measurements:
    def __init__(self):
        self.frames = self.cells = 0
        self.first = None
        self.first_frame = None

    def append_trajectory(self, archive, sequence, frames):
        self.frames += len(frames)
        if self.first_frame is None:
            self.first_frame = frames[0]

    def append_observations(self, archive, sequence, observations):
        self.cells += len(observations['cells'])
        if self.first is None:
            self.first = {row['regionId']: row['population'] for row in observations['cells']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--native-seconds', type=int, default=0)
    parser.add_argument('--binary')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    repo = MongoRepository()
    try:
        reader = DuringArchiveReader(repo.db)
        report = reader.report(args.run_id, 'readonly-f00-audit')
        rows, metadata = import_archive(reader, report['sourceBinding'], 'readonly-f00-audit')
        dataset = {'observations': rows, 'metadata': metadata}
        context = metadata['factualContext']
        result = {'runId': args.run_id, 'sourceVersion': report['sourceVersion'], 'databaseWrites': False,
            'observationCount': len(rows), 'regionCount': len(context['expectedRegionIds']),
            'sampleCount': len(context['expectedTimes']), 'samplingSeconds': context['samplingSeconds'],
            'gridSizeMeters': context['geometry']['parameters']['grid_size_m'],
            'areaM2': context['geometry']['area_m2'], 'initialPeople': len(context['initialDistribution'])}
        with TemporaryDirectory(prefix='post-f00-smoke-') as folder:
            folder = Path(folder)
            manifest = compile_routes(dataset, 42, [], metadata['durationSeconds'], folder / 'demand.rou.xml')
            result['initialEdgeCounts'] = dict(Counter(p['edgeId'] for p in manifest['cohorts']))
            result['distinctSpeeds'] = len({p['profile']['free_walking_speed'] * p['profile']['mobility'] for p in manifest['cohorts']})
            if args.native_seconds:
                from crowdsim.infrastructure.sumo_adapter import discover_sumo_binary
                duration = args.native_seconds
                binary = discover_sumo_binary(args.binary)
                fcd = folder / 'fcd.xml'
                command = sumo_parameters(context) + ['--begin', '0', '--end', str(duration), '--seed', '42',
                    '--fcd-output', str(fcd), '--person-device.fcd.probability', '1',
                    '--device.fcd.period', str(context['samplingSeconds']), '--person-device.fcd.period', str(context['samplingSeconds']),
                    '--precision', '8', '--no-step-log', 'true']
                events = execute(binary, folder / 'demand.rou.xml', folder, command, manifest, duration, lambda: False)
                measured = Measurements()
                output = measure_distribution(fcd, duration, {}, measured, dataset, control_events=events)
                target = {r['regionId']: r['population'] for r in rows if r['timeSeconds'] == context['startSeconds']}
                result['nativeSmoke'] = {'durationSeconds': duration, 'frames': measured.frames,
                    'cellRows': measured.cells, 'firstCellPopulationAbsoluteError': sum(abs(count - measured.first[key]) for key, count in target.items()),
                    'measurement': output['measurement'], 'calibration': output['calibration']}
                from shapely.geometry import Point, shape
                shapes = [(cell['cell_id'], shape(cell['geometry'])) for cell in context['geometry']['cells']]
                expected_people = {person['id']: person for person in manifest['cohorts']}
                result['nativeSmoke']['initialPlacementMismatches'] = [
                    {'id': person['id'], 'source': expected_people[person['id']], 'actual': person}
                    for person in measured.first_frame['persons']
                    if not next(polygon for key, polygon in shapes if key == expected_people[person['id']]['regionId']).covers(Point(person['x'], person['y']))]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({key: value for key, value in result.items() if key != 'initialEdgeCounts'}, ensure_ascii=True))
    finally:
        repo.close()


if __name__ == '__main__':
    main()
