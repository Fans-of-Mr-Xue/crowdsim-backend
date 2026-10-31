"""One shared MongoDB collection for crowd, response and vehicle subjects."""
import json
import re
from pathlib import Path
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .datasets import DatasetConflictError
from .knowledge_documents import now
from ..schemas.subjects import validate_subject

SEEDS = Path(__file__).resolve().parents[1] / 'resources' / 'subject_profiles.json'


class SubjectRepository:
    def __init__(self, database):
        self.collection = database['subject_profiles']

    def initialize(self):
        self.collection.create_index([('is_deleted', 1), ('sort_order', 1)])
        self.collection.create_index('name', unique=True, partialFilterExpression={'is_deleted': False})
        self.collection.create_index([('topic', 1), ('is_deleted', 1)])
        for index, seed in enumerate(json.loads(SEEDS.read_text(encoding='utf-8'))):
            identifier = seed.pop('id')
            if self.collection.find_one({'_id': identifier}):
                continue  # Never overwrite edited records or restore deleted seeds.
            timestamp = now()
            document = {'_id': identifier, **validate_subject(seed), 'origin': 'builtin', 'revision': 1,
                        'is_deleted': False, 'sort_order': index, 'created_at': timestamp, 'updated_at': timestamp}
            try:
                self.collection.update_one({'_id': identifier}, {'$setOnInsert': document}, upsert=True)
            except DuplicateKeyError:
                # A user may already have created a subject with this name.
                continue

    @staticmethod
    def public(document):
        if document is None:
            return None
        return {'id': document['_id'], **{key: value for key, value in document.items() if key not in ('_id', 'is_deleted', 'sort_order')}}

    def list(self, search=''):
        query = {'is_deleted': {'$ne': True}}
        if search:
            query['name'] = {'$regex': re.escape(search), '$options': 'i'}
        documents = list(self.collection.find(query).sort([('sort_order', 1), ('created_at', -1)]))
        crowd = [d for d in documents if d.get('topic') == '人群主体']
        return {'items': [self.public(d) for d in documents], 'stats': {
            'total': len(documents), 'crowd': len(crowd), 'support': len(documents) - len(crowd),
            'behaviorDimensions': sum(len(d.get('agentPortrait', {}).get('metrics', [])) for d in crowd),
        }}

    def get(self, identifier):
        return self.public(self.collection.find_one({'_id': identifier, 'is_deleted': {'$ne': True}}))

    def save(self, payload, identifier=None, revision=None):
        data = validate_subject(payload)
        timestamp = now()
        try:
            if identifier:
                if not self.get(identifier):
                    raise KeyError(identifier)
                document = self.collection.find_one_and_update(
                    {'_id': identifier, 'revision': revision, 'is_deleted': False},
                    {'$set': {**data, 'updated_at': timestamp}, '$inc': {'revision': 1}},
                    return_document=ReturnDocument.AFTER,
                )
                if document is None:
                    raise DatasetConflictError('主体已被其他用户修改，请刷新后重试；当前修改仍保留在页面中')
            else:
                document = {'_id': uuid4().hex, **data, 'origin': 'custom', 'revision': 1,
                            'is_deleted': False, 'sort_order': 1000, 'created_at': timestamp, 'updated_at': timestamp}
                self.collection.insert_one(document)
        except DuplicateKeyError:
            raise DatasetConflictError('同名主体已存在，请修改名称或编辑已有主体') from None
        return self.public(document)

    def delete(self, identifier, revision):
        if self.get(identifier) is None:
            raise KeyError(identifier)
        result = self.collection.update_one({'_id': identifier, 'revision': revision, 'is_deleted': False},
                                            {'$set': {'is_deleted': True, 'updated_at': now()}, '$inc': {'revision': 1}})
        if not result.matched_count:
            raise DatasetConflictError('主体已更新或删除，请刷新后重试')
