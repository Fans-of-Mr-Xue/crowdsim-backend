"""Immutable environment road editions; assets are retained for pinned tasks."""
import json
from gridfs import GridFSBucket
from .knowledge_documents import now


class EnvironmentRoadVersionRepository:
    def __init__(self, database):
        self.collection = database['environment_road_versions']
        self.assets = GridFSBucket(database, bucket_name='environment_assets')

    def capture(self, environment, roads=None):
        if not environment.get('roads_file_id'):
            return None
        key = f"{environment['_id']}:{environment['revision']}"
        existing = self.collection.find_one({'_id': key})
        if existing:
            return existing
        if roads is None:
            with self.assets.open_download_stream(environment['roads_file_id']) as stream:
                roads = json.load(stream)
        doc = {'environment_id': environment['_id'], 'environment_revision': environment['revision'],
               'file_id': environment['roads_file_id'], 'boundary': environment['geometry'],
               'source': roads.get('source', {}), 'quality': roads.get('quality', {}),
               'feature_count': len(roads['features']), 'created_at': now()}
        self.collection.update_one({'_id': key}, {'$setOnInsert': doc}, upsert=True)
        return self.collection.find_one({'_id': key})

    def get(self, identifier, revision):
        return self.collection.find_one({'_id': f'{identifier}:{revision}'})

    def read(self, identifier, revision):
        record = self.get(identifier, revision)
        if record is None:
            raise KeyError('environment road version')
        with self.assets.open_download_stream(record['file_id']) as stream:
            return json.load(stream)

    def list(self, identifier):
        return [{key: value for key, value in doc.items() if key not in ('file_id', 'boundary')} for doc in
                self.collection.find({'environment_id': identifier}).sort('environment_revision', -1).limit(100)]
