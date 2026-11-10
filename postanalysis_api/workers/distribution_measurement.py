"""Measure F00 on the original clipped observation cells and sample clock."""
from collections import defaultdict
import math
from statistics import mean
import xml.etree.ElementTree as ET


def measure_distribution(fcd_path, duration, archive, repository, dataset, *, cancelled=lambda: False, control_events=()):
    from shapely.geometry import Point, shape
    from crowdsim.domain.observation_config import ObservationConfig
    from crowdsim.infrastructure.observation_metrics import ObservationCollector
    context = dataset['metadata']['factualContext']
    geometry = context['geometry']
    collector = ObservationCollector(shape(geometry['scope_geometry']).exterior.coords,
        ['global-density', 'global-speed', 'local-density', 'local-speed', 'boundary-density-difference'],
        ObservationConfig(**geometry['parameters']))
    interval, step = context['samplingSeconds'], context['stepSeconds']
    expected = [time for time in context['expectedTimes'] if time <= duration]
    expected_set, observed = set(expected), set()
    targets = {(row['timeSeconds'], row['regionId']): row for row in dataset['observations']}
    errors = {key: {'count': 0, 'absoluteSum': 0., 'squaredSum': 0., 'maxAbsolute': 0.}
              for key in ('population', 'density', 'meanSpeed')}
    timeline, frames, measured, sequence, chunk = [], [], [], 0, 0
    region_samples, congested = defaultdict(list), defaultdict(float)
    peak_density = peak_pressure = risk_exposure = congestion = speed_sum = person_time = 0.
    risk_ids, all_ids = set(), set()
    cfg = collector.config
    events, event_index, planned_waiting = sorted(control_events, key=lambda row: row['timeSeconds']), 0, set()
    for _event, element in ET.iterparse(fcd_path, events=('end',)):
        if element.tag != 'timestep':
            continue
        if cancelled():
            raise InterruptedError('Post run cancelled')
        # SUMO labels FCD with the beginning of the completed integration step.
        # During snapshots use simulation.getTime(), i.e. its end.
        time = round(float(element.get('time')) + step, 9)
        if time not in expected_set:
            element.clear(); continue
        if time in observed:
            raise ValueError('Duplicate FCD observation sample')
        observed.add(time)
        while event_index < len(events) and events[event_index]['timeSeconds'] <= time:
            event = events[event_index]
            if event['type'] == 'hold':
                planned_waiting.add(event['personId'])
            elif event['type'] in {'release', 'release_before_hold'}:
                planned_waiting.discard(event['personId'])
            event_index += 1
        people, cells, bands = [], defaultdict(list), defaultdict(int)
        for node in element.findall('person'):
            item = node.attrib
            x, y, speed = float(item['x']), float(item['y']), float(item['speed'])
            angle = math.radians(float(item.get('angle', 0)))
            person = {'id': item['id'], 'x': x, 'y': y, 'speed': speed,
                      'edgeId': item.get('edge', item.get('lane', '').rsplit('_', 1)[0]),
                      'vx': speed * math.sin(angle), 'vy': speed * math.cos(angle)}
            if not all(math.isfinite(value) for value in (x, y, speed)) or speed < 0:
                raise ValueError('Invalid FCD position or speed')
            people.append(person); all_ids.add(person['id'])
            point = Point(x, y)
            if not collector.scope.covers(point):
                continue
            cell_id = collector._cell_for(point)
            if cell_id is None:
                raise ValueError('In-scope person has no original observation cell')
            cells[cell_id].append(person)
            if collector._band_tree is not None:
                for index in collector._band_tree.query(point, predicate='covered_by'):
                    boundary_index, side, owner = collector._band_refs[index]
                    if owner == cell_id:
                        bands[(boundary_index, side)] += 1
        scoped = [person for occupants in cells.values() for person in occupants]
        frame_peak = frame_pressure = frame_risk = 0.
        active_congested = set()
        dt = interval
        for cell in collector.cells.values():
            cell_id = cell['cell_id']
            occupants = cells[cell_id]
            unplanned = [person for person in occupants if person['id'] not in planned_waiting]
            count, area = len(occupants), cell['area_m2']
            density = count / area
            speed = mean(person['speed'] for person in occupants) if occupants else None
            vx = mean(person['vx'] for person in occupants) if occupants else 0
            vy = mean(person['vy'] for person in occupants) if occupants else 0
            pressure = density * mean((p['vx'] - vx) ** 2 + (p['vy'] - vy) ** 2 for p in occupants) if occupants else 0
            local = {'timeSeconds': time, 'regionId': cell_id, 'population': count, 'density': density,
                     'meanSpeed': speed, 'pressureProxy': pressure,
                     'lowSpeedPopulation': sum(p['speed'] < cfg.low_speed_threshold_mps for p in occupants)}
            local['plannedWaitingPopulation'] = count - len(unplanned)
            target = targets[(time, cell_id)]
            residuals = {}
            for field, stats in errors.items():
                if target.get(field) is not None and local.get(field) is not None:
                    value = local[field] - target[field]
                    stats['count'] += 1; stats['absoluteSum'] += abs(value); stats['squaredSum'] += value * value
                    stats['maxAbsolute'] = max(stats['maxAbsolute'], abs(value))
                    residuals[field] = value
            measured.append({**local, 'sourceSnapshotId': target.get('sourceSnapshotId'), 'residuals': residuals})
            frame_peak, frame_pressure = max(frame_peak, density), max(frame_pressure, pressure)
            if density >= cfg.crowded_density_person_per_m2:
                frame_risk += count; risk_ids.update(p['id'] for p in occupants)
                if unplanned and mean(p['speed'] for p in unplanned) < cfg.low_speed_threshold_mps:
                    active_congested.add(cell_id)
        for cell_id in list(congested):
            if cell_id not in active_congested:
                if congested[cell_id] >= cfg.blocked_confirmation_seconds:
                    congestion += congested[cell_id]
                del congested[cell_id]
        for cell_id in active_congested:
            congested[cell_id] += dt
        boundaries = []
        for index, boundary in enumerate(collector.boundaries):
            areas = [boundary[f'band_{side}_area_m2'] for side in ('a', 'b')]
            densities = [bands[index, side] / area if area else None for side, area in zip(('a', 'b'), areas)]
            difference = abs(densities[0] - densities[1]) if all(value is not None for value in densities) else None
            boundaries.append({'boundaryId': boundary['boundary_id'], 'absoluteDensityDifference': difference,
                               'status': 'valid' if difference is not None else 'outside_not_observed'})
        row = {'timeSeconds': time, 'population': len(scoped), 'density': len(scoped) / collector.scope.area,
               'peakDensity': frame_peak, 'meanSpeed': mean(p['speed'] for p in scoped) if scoped else None,
               'pressureProxy': frame_pressure, 'riskPopulation': frame_risk}
        timeline.append(row); region_samples['global'].append(row)
        peak_density, peak_pressure = max(peak_density, frame_peak), max(peak_pressure, frame_pressure)
        risk_exposure += frame_risk * dt
        speed_sum += sum(p['speed'] for p in scoped) * dt; person_time += len(scoped) * dt
        frames.append({'timeSeconds': time, 'persons': people})
        if len(frames) >= 5:
            repository.append_trajectory(archive, sequence, frames)
            sequence += 1; frames = []
        # Full cell measurements are archived in bounded chunks, not a 16 MiB result document.
        repository.append_observations(archive, chunk, {'cells': measured, 'timeSeconds': time, 'boundaries': boundaries})
        chunk += 1; measured = []
        element.clear()
    if frames:
        repository.append_trajectory(archive, sequence, frames)
    if observed != expected_set:
        raise ValueError(f'FCD sample clock incomplete: missing {len(expected_set - observed)} samples')
    congestion += sum(value for value in congested.values() if value >= cfg.blocked_confirmation_seconds)
    summary = {field: {'count': stats['count'], 'mae': stats['absoluteSum'] / stats['count'] if stats['count'] else None,
                      'rmse': math.sqrt(stats['squaredSum'] / stats['count']) if stats['count'] else None,
                      'maxAbsolute': stats['maxAbsolute'] if stats['count'] else None} for field, stats in errors.items()}
    return {'metrics': {'peak_density': peak_density, 'mean_speed': speed_sum / person_time if person_time else None,
                        'peak_pressure_proxy': peak_pressure, 'risk_exposed_unique': len(risk_ids),
                        'risk_exposure': risk_exposure, 'congestion_duration': congestion, 'evacuation_time': None},
            'timeline': timeline, 'regionSamples': dict(region_samples),
            'calibration': {'calibrated': False, 'status': 'spatial_residuals_measured_acceptance_not_configured',
                            'populationMAE': summary['population']['mae'], 'spatialErrors': summary,
                            'observationCount': errors['population']['count'], 'observationChunks': chunk},
            'measurement': {'gridSizeMeters': cfg.grid_size_m, 'samplingSeconds': interval,
                'startSeconds': expected[0] if expected else None, 'endSeconds': expected[-1] if expected else None,
                'gridOriginXY': geometry['grid_origin_xy'], 'areaM2': collector.scope.area,
                'areaDefinition': 'original during selected polygon clipped observation cells',
                'globalDensityDefinition': 'in-scope population / selected polygon area',
                'peakDensityDefinition': 'maximum cell population / original clipped cell area',
                'riskDensityThreshold': cfg.crowded_density_person_per_m2,
                'lowSpeedThreshold': cfg.low_speed_threshold_mps, 'confirmationSeconds': cfg.blocked_confirmation_seconds,
                'plannedWaitingExcludedFromCongestion': True,
                'fcdClockOffsetSeconds': step, 'observedPersonCount': len(all_ids), 'evacuationCensored': True}}
