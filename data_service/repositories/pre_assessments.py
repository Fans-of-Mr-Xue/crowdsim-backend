"""Immutable task-linked prediction runs, with large results in compressed GridFS."""
import gzip
import hashlib
import io
import json
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from gridfs import GridFSBucket
from .knowledge_documents import now
from ..prediction.catalog import method_catalog
from ..prediction.runtime_budget import runtime_budget


def input_key(task, settings, methods, road_id):
    catalog = {item['id']: item for item in method_catalog(task, include_legacy=True)}
    versions = [(method, catalog[method]['version'], catalog[method]['formula_hash']) if catalog[method].get('custom') else (method, catalog[method]['version']) for method in methods]
    payload = {'task_id': task['id'], 'revision': task['revision'], 'settings': settings,
               'methods': sorted(versions),
               'road_id': road_id, 'solver_version': '3.2.1-full-window-stable-force', 'runtime_budget': runtime_budget()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def lease_expiry():
    return (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()


class PreAssessmentRepository:
    def __init__(self, database):
        self.collection = database['pre_assessment_runs']
        self.results = GridFSBucket(database, bucket_name='pre_assessment_results')

    def initialize(self):
        self.collection.create_index([('task_id', 1), ('task_revision', 1), ('created_at', -1)])
        self.collection.create_index([('task_id', 1), ('status', 1), ('created_at', -1)])
        self.collection.create_index([('status', 1), ('owner', 1)])
        self.collection.create_index([('input_key', 1), ('created_at', -1)])

    @staticmethod
    def public(doc):
        return {'id': doc['_id'], **{key: value for key, value in doc.items() if key not in ('_id', 'result_file_id', 'owner')}}

    def create(self, task, settings, methods, owner, key, road_source):
        doc = {'_id': uuid4().hex, 'task_id': task['id'], 'task_revision': task['revision'],
               'task_title': task['title'], 'methods': methods, 'settings': settings,
               'input_key': key, 'road_source': road_source, 'method_definitions': [item for item in method_catalog(task, include_legacy=True) if item['id'] in methods],
               'status': 'queued', 'owner': owner, 'created_at': now(), 'updated_at': now(), 'lease_expires_at': lease_expiry()}
        self.collection.insert_one(doc)
        return self.public(doc)

    def get(self, identifier):
        self.collection.update_one({'_id': identifier, 'status': {'$in': ['queued', 'running']}, 'lease_expires_at': {'$lt': now()}},
            {'$set': {'status': 'failed', 'error': '预测服务已中断或失去连接，请重新计算', 'error_code': 'lease_expired', 'updated_at': now()}})
        doc = self.collection.find_one({'_id': identifier})
        return self.public(doc) if doc else None

    def latest(self, task_id, revision=None, *, prefer_completed=False):
        query = {'task_id': task_id}
        if revision is not None:
            query['task_revision'] = revision
        # Task edits create a revision, not a new task. Keep completed experiments
        # discoverable without relabelling their immutable task/road snapshots.
        # Results pages show a completed run while the console follows active work.
        priorities = (['completed'], ['queued', 'running'], ['failed']) if prefer_completed else (['queued', 'running'], ['completed'], ['failed'])
        for statuses in priorities:
            for doc in self.collection.find({**query, 'status': {'$in': statuses}}).sort('created_at', -1):
                run = self.get(doc['_id'])
                if run['status'] in statuses:
                    return run
        return None

    def reusable(self, key):
        for statuses in (['queued', 'running'], ['completed']):
            for doc in self.collection.find({'input_key': key, 'status': {'$in': statuses}}).sort('created_at', -1):
                run = self.get(doc['_id'])
                if run['status'] in statuses:
                    return run
        return None

    def update(self, identifier, status, **fields):
        query = {'_id': identifier, 'status': {'$in': ['queued', 'running']}}
        update = {'$set': {'status': status, 'updated_at': now(), 'lease_expires_at': lease_expiry(), **fields}}
        if status == 'running':
            # Only the live executor calls this. A lost DB heartbeat is not proof
            # its CPU worker stopped; allow it to reclaim its own expired run.
            query = {'_id': identifier, '$or': [{'status': {'$in': ['queued', 'running']}},
                                               {'status': 'failed', 'error_code': 'lease_expired'}]}
            update['$unset'] = {'error': '', 'error_code': ''}
        return self.collection.update_one(query, update)

    def heartbeat(self, identifier):
        self.collection.update_one({'_id': identifier, 'status': {'$in': ['queued', 'running']}}, {'$set': {'lease_expires_at': lease_expiry()}})

    def complete(self, identifier, result):
        result['run_id'] = identifier
        result['generated_at'] = now()
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
        file_id = self.results.upload_from_stream(identifier + '.json.gz', io.BytesIO(gzip.compress(encoded)),
            metadata={'content_type': 'application/json', 'encoding': 'gzip'})
        summaries = [{'method': item['method'], 'summary': item['summary']} for item in result['results']]
        updated = self.collection.update_one({'_id': identifier, '$or': [{'status': 'running'}, {'status': 'failed', 'error_code': 'lease_expired'}]},
            {'$set': {'result_file_id': file_id, 'summaries': summaries, 'status': 'completed', 'updated_at': now()},
             '$unset': {'error': '', 'error_code': ''}})
        if not updated.modified_count:
            self.results.delete(file_id)

    def read_result(self, identifier):
        doc = self.collection.find_one({'_id': identifier, 'status': 'completed'})
        if not doc or not doc.get('result_file_id'):
            raise KeyError(identifier)
        with self.results.open_download_stream(doc['result_file_id']) as source:
            return json.loads(gzip.decompress(source.read()))

    def interrupt_owner(self, owner):
        self.collection.update_many({'owner': owner, 'status': {'$in': ['queued', 'running']}},
            {'$set': {'status': 'failed', 'error': '数据服务已停止，本次计算未发布，请重新计算', 'updated_at': now()}})
