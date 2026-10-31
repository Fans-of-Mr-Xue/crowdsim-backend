"""Immutable road snapshots for a task boundary, separate from task definition."""
import hashlib
import io
import json
from uuid import uuid4
from gridfs import GridFSBucket
from .knowledge_documents import now


def boundary_key(task):
    encoded = json.dumps(task['region']['geometry'], sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


class TaskRoadRepository:
    def __init__(self, database):
        self.collection = database['analysis_task_road_networks']
        self.heads = database['analysis_task_road_heads']
        self.assets = GridFSBucket(database, bucket_name='analysis_task_road_assets')

    def initialize(self):
        self.collection.create_index([('task_id', 1), ('boundary_key', 1), ('created_at', -1)])

    @staticmethod
    def public(doc):
        return {key: value for key, value in doc.items() if key not in ('file_id', '_id')} | {'id': doc['_id']}

    def latest(self, task):
        region = task['region']
        head = self.heads.find_one({'_id': self.head_key(task)})
        if head:
            return self.get(head['snapshot_id'])
        for doc in self.collection.find({'task_id': task['id'], 'boundary_key': boundary_key(task)}).sort('created_at', -1):
            binding = doc.get('region_binding')
            expected = {key: region.get(key) for key in ('mode', 'environment_id', 'environment_revision')}
            if doc.get('road_binding', {}) != task.get('components', {}).get('road_network_binding', {}):
                continue
            if binding is not None and binding != expected:
                continue
            if binding is None and region['mode'] == 'environment':
                sources = doc.get('source', {}).get('environments', [])
                if not any(item.get('environment_id') == region.get('environment_id') and item.get('environment_revision') == region.get('environment_revision') for item in sources):
                    continue
            self.heads.update_one({'_id': self.head_key(task)}, {'$setOnInsert': {'task_id': task['id'], 'snapshot_id': doc['_id']}}, upsert=True)
            return self.get(self.heads.find_one({'_id': self.head_key(task)})['snapshot_id'])
        return None

    @staticmethod
    def head_key(task):
        binding = {key: task['region'].get(key) for key in ('mode', 'environment_id', 'environment_revision')}
        binding['road_network_binding'] = task.get('components', {}).get('road_network_binding', {})
        digest = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
        return task['id'] + ':' + boundary_key(task) + ':' + digest

    def read(self, identifier):
        doc = self.collection.find_one({'_id': identifier})
        if not doc:
            raise KeyError(identifier)
        with self.assets.open_download_stream(doc['file_id']) as stream:
            return json.loads(stream.read())

    def get(self, identifier):
        doc = self.collection.find_one({'_id': identifier})
        return self.public(doc) if doc else None

    def save(self, task, roads, source, *, replace_snapshot=None):
        identifier = uuid4().hex
        file_id = self.assets.upload_from_stream(identifier + '.geojson', io.BytesIO(json.dumps(roads, ensure_ascii=False).encode()))
        doc = {'_id': identifier, 'task_id': task['id'], 'boundary_key': boundary_key(task),
               'region_binding': {key: task['region'].get(key) for key in ('mode', 'environment_id', 'environment_revision')},
               'road_binding': task.get('components', {}).get('road_network_binding', {}),
               'file_id': file_id, 'source': source, 'feature_count': len(roads['features']), 'created_at': now()}
        try:
            self.collection.insert_one(doc)
        except Exception:
            if not self.collection.count_documents({'_id': identifier}):
                self.assets.delete(file_id)
            raise
        # The data API and independent crawler API may resolve the same task at
        # once. An atomic pointer makes both return the same canonical snapshot.
        head_key = self.head_key(task)
        if replace_snapshot:
            self.heads.update_one({'_id': head_key, 'snapshot_id': replace_snapshot}, {'$set': {'snapshot_id': identifier}})
        else:
            self.heads.update_one({'_id': head_key}, {'$setOnInsert': {'task_id': task['id'], 'snapshot_id': identifier}}, upsert=True)
        return self.get(self.heads.find_one({'_id': head_key})['snapshot_id'])
