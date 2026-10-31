"""A stable task ID points atomically to immutable, separate detail snapshots.

Writing details before a compare-and-swap on the task document works on standalone
MongoDB too. Readers resolve one definition_id, so they never see mixed revisions.
"""
import copy
import re
from uuid import uuid4
from pymongo import ReturnDocument
from .datasets import DatasetConflictError
from .environments import EnvironmentRepository
from .knowledge_documents import now
from ..schemas.analysis_tasks import validate_task, validate_component
from ..schemas.environments import geometry_bounds, area_km2

COLLECTIONS = {'region': 'analysis_task_regions', 'population': 'analysis_task_population', 'references': 'analysis_task_references'}


class AnalysisTaskRepository:
    def __init__(self, database):
        self.database = database
        self.tasks = database['analysis_tasks']
        self.details = {name: database[collection] for name, collection in COLLECTIONS.items()}
        self.components = database['analysis_task_components']
        self.environments = EnvironmentRepository(database)

    def initialize(self):
        self.tasks.create_index([('is_deleted', 1), ('updated_at', -1)])
        for collection in self.details.values():
            collection.create_index('task_id')
        self.components.create_index([('definition_id', 1), ('name', 1)], unique=True)
        self.components.create_index('task_id')

    @staticmethod
    def public(doc):
        return {'id': doc['_id'], **{k: v for k, v in doc.items() if k not in ('_id', 'definition_id', 'is_deleted')}}

    def get(self, identifier):
        doc = self.tasks.find_one({'_id': identifier, 'is_deleted': {'$ne': True}})
        if doc is None:
            return None
        result = self.public(doc)
        definition = doc['definition_id']
        for name, collection in self.details.items():
            detail = collection.find_one({'_id': definition})
            if detail is None:
                raise DatasetConflictError('任务详情暂不可读，请重新加载')
            result[name] = detail['data']
        result['components'] = {d['name']: d['data'] for d in self.components.find({'definition_id': definition})}
        return result

    def list(self, page=1, size=20, search=''):
        query = {'is_deleted': {'$ne': True}}
        if search:
            query['title'] = {'$regex': re.escape(search), '$options': 'i'}
        items = self.tasks.find(query).sort([('updated_at', -1), ('_id', 1)]).skip((page - 1) * size).limit(size)
        return {'items': [self.public(doc) for doc in items], 'total': self.tasks.count_documents(query), 'page': page, 'page_size': size}

    def region_snapshot(self, region, previous=None):
        if region['mode'] == 'environment':
            identifier, revision = region['environment_id'], region['environment_revision']
            # Existing tasks preserve their boundary even after the source is edited/deleted.
            if previous and previous.get('mode') == 'environment' and previous.get('environment_id') == identifier and previous.get('environment_revision') == revision:
                return copy.deepcopy(previous)
            environment = self.environments.get(identifier)
            if environment is None:
                raise ValueError('所选环境库区域不存在，请重新选择')
            if environment['revision'] != revision:
                raise DatasetConflictError('环境库区域已更新，请刷新环境库后重新选择')
            region = {'mode': 'environment', 'environment_id': identifier, 'environment_revision': revision,
                      'geometry': environment['geometry'], 'center': list(reversed(environment['coordinates'])),
                      'name': environment['title'], 'boundary_source': environment.get('boundarySource', {}),
                      'profile_snapshot': {k: environment[k] for k in ('id', 'revision', 'title', 'cityCode', 'locationLabel', 'description', 'assessmentProfile', 'dashboard', 'regionLevel', 'roadsCount') if k in environment},
                      'zoom': region.get('zoom', 15)}
        bounds = geometry_bounds(region['geometry'])
        region.update(coordinate_system='WGS84', area_km2=area_km2(region['geometry']),
                      bounds=bounds, center=region.get('center') or [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2])
        return region

    def save(self, payload, identifier=None, revision=None, *, confirm_domain=False):
        data = validate_task(payload)
        if 'custom_methods' in data['components']:
            from ..prediction.custom_formulas import normalize_custom_methods
            data['components']['custom_methods'] = normalize_custom_methods(data['components']['custom_methods'])
        previous = self.get(identifier) if identifier else None
        if identifier and previous is None:
            raise KeyError(identifier)
        if previous and previous['revision'] != revision:
            raise DatasetConflictError('任务已被其他用户修改，请重新加载后再保存')
        # Keep the requirement console and the later run editor on one set of
        # settings. Only an explicit experiment edit overrides a saved run setup.
        components = data['components']
        experiment = components.get('research_plan', {}).get('experiment')
        old_experiment = (previous or {}).get('components', {}).get('research_plan', {}).get('experiment', {})
        if 'forecast_setup' in components:
            from ..prediction.inputs import normalize_settings
            from ..prediction.consultation_catalog import experiment_defaults
            if experiment is not None:
                if experiment.get('settings') != old_experiment.get('settings', {}):
                    defaults = experiment_defaults()
                    removed = set(old_experiment.get('settings', {})) - set(experiment['settings'])
                    components['forecast_setup'].update({key: defaults[key] for key in removed})
                    components['forecast_setup'].update(experiment['settings'])
                else:
                    experiment['settings'] = {key: components['forecast_setup'][key] for key in experiment_defaults()
                                              if key in components['forecast_setup']}
            components['forecast_setup'] = normalize_settings(data, components['forecast_setup'])
        data['region'] = self.region_snapshot(data['region'], previous.get('region') if previous else None)
        from ..schemas.domain_model import model_fingerprint, reconcile_review
        if confirm_domain:
            review = data['components']['domain_model_review']
            review.update(status='confirmed', confirmed_at=now(), confirmed_fingerprint=model_fingerprint(data))
        else:
            reconcile_review(data)
        identifier = identifier or uuid4().hex
        definition = uuid4().hex
        timestamp = now()
        try:
            for name, collection in self.details.items():
                collection.insert_one({'_id': definition, 'task_id': identifier, 'schema_version': 1, 'data': data[name], 'created_at': timestamp})
            extras = data['components']
            if extras:
                self.components.insert_many([{'task_id': identifier, 'definition_id': definition, 'name': name, 'schema_version': 1, 'data': value, 'created_at': timestamp} for name, value in extras.items()])
            metadata = {k: v for k, v in data.items() if k not in (*COLLECTIONS, 'components')}
            metadata.update(definition_id=definition, schema_version=1, updated_at=timestamp)
            if previous:
                doc = self.tasks.find_one_and_update({'_id': identifier, 'revision': revision, 'is_deleted': {'$ne': True}},
                    {'$set': metadata, '$inc': {'revision': 1}}, return_document=ReturnDocument.AFTER)
                if doc is None:
                    raise DatasetConflictError('任务正在更新，请重新加载后再保存')
            else:
                doc = {'_id': identifier, **metadata, 'revision': 1, 'is_deleted': False, 'created_at': timestamp}
                self.tasks.insert_one(doc)
        except Exception:
            # A disconnected write may have committed; retain the snapshot in that case.
            if not self.tasks.count_documents({'_id': identifier, 'definition_id': definition}):
                for collection in self.details.values():
                    collection.delete_one({'_id': definition})
                self.components.delete_many({'definition_id': definition})
            raise
        return {**self.public(doc), **{k: data[k] for k in COLLECTIONS}, 'components': data['components']}

    def put_component(self, identifier, name, value, revision):
        validate_component(name, value)
        current = self.get(identifier)
        if current is None:
            raise KeyError(identifier)
        if current['revision'] != revision:
            raise DatasetConflictError('任务已更新，请重新加载')
        if name == 'forecast_setup':
            from ..prediction.inputs import normalize_settings
            value = normalize_settings(current, value)
        payload = {k: current[k] for k in ('title', 'summary', 'event_id', 'event_label', 'time_window', 'template_case', 'status', *COLLECTIONS, 'components')}
        payload['components'][name] = value
        return self.save(payload, identifier, revision)

    def delete(self, identifier, revision):
        result = self.tasks.update_one({'_id': identifier, 'revision': revision, 'is_deleted': {'$ne': True}},
            {'$set': {'is_deleted': True, 'updated_at': now()}, '$inc': {'revision': 1}})
        if not result.matched_count:
            raise DatasetConflictError('任务已更新或删除，请重新加载')
