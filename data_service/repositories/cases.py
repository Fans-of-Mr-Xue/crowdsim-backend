"""Persistent cases and versioned HTML detail attachments stored in GridFS."""
import hashlib
import io
import json
from pathlib import Path
from uuid import uuid4

from bs4 import BeautifulSoup
from gridfs import GridFSBucket
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .datasets import DatasetConflictError
from .knowledge_documents import now
from ..schemas.cases import validate_case, validate_links

RESOURCES = Path(__file__).resolve().parents[1] / 'resources'
MAX_PAGE_BYTES = 16 * 1024 * 1024


class CaseRepository:
    def __init__(self, database):
        self.collection = database['case_profiles']
        self.pages = GridFSBucket(database, bucket_name='case_pages')

    def initialize(self):
        self.collection.create_index([('is_deleted', 1), ('sort_order', 1)])
        self.collection.create_index('name', unique=True, partialFilterExpression={'is_deleted': False})
        self.collection.create_index([('eventId', 1), ('is_deleted', 1)])
        self.collection.create_index([('topic', 1), ('locationType', 1)])
        for index, seed in enumerate(json.loads((RESOURCES / 'case_profiles.json').read_text(encoding='utf-8'))):
            identifier = seed.pop('id')
            if not self.collection.find_one({'_id': identifier}):
                timestamp = now()
                document = {'_id': identifier, **validate_case(seed), 'origin': 'builtin', 'revision': 1,
                            'is_deleted': False, 'sort_order': index, 'created_at': timestamp, 'updated_at': timestamp,
                            'detail_pages': [], 'active_page_id': '', 'builtin_page_seeded': False}
                try:
                    self.collection.update_one({'_id': identifier}, {'$setOnInsert': document}, upsert=True)
                except DuplicateKeyError:
                    continue
            if identifier == 'madras-lyon-2022':
                document = self.collection.find_one({'_id': identifier})
                if document and not document['is_deleted'] and not document.get('builtin_page_seeded'):
                    if not document.get('detail_pages'):
                        try:
                            raw = (RESOURCES / 'case_pages' / (identifier + '.html')).read_bytes()
                            self.attach_page(identifier, document['revision'], io.BytesIO(raw), '里昂灯光节-MADRAS.html', origin='builtin')
                        except DatasetConflictError:
                            continue  # Another initializer/editor attached the page first.
                    self.collection.update_one({'_id': identifier}, {'$set': {'builtin_page_seeded': True}})

    @staticmethod
    def public(document):
        if document is None:
            return None
        hidden = {'_id', 'is_deleted', 'sort_order', 'detail_pages', 'active_page_id', 'builtin_page_seeded'}
        data = {key: value for key, value in document.items() if key not in hidden}
        pages = [{**{key: value for key, value in page.items() if key != 'file_id'},
                  'url': f"/api/crowdSim/cases/{document['_id']}/pages/{page['id']}"} for page in document.get('detail_pages', [])]
        return {'id': document['_id'], **data, 'detailPages': pages,
                'detailPage': next((page for page in pages if page['id'] == document.get('active_page_id')), None)}

    def get(self, identifier):
        return self.public(self.collection.find_one({'_id': identifier, 'is_deleted': {'$ne': True}}))

    def list(self):
        documents = list(self.collection.find({'is_deleted': {'$ne': True}}).sort([('sort_order', 1), ('created_at', -1)]))
        crowd = sum(d.get('topic') in ('人群踩踏', '大型活动') for d in documents)
        return {'items': [self.public(d) for d in documents], 'stats': {'total': len(documents),
                'reviewed': sum(d.get('status') == '已复盘' for d in documents), 'crowd': crowd,
                'flood': sum(d.get('topic') in ('城市内涝', '暴雨洪涝') for d in documents)}}

    def change(self, identifier, revision, update):
        if self.get(identifier) is None:
            raise KeyError(identifier)
        update.setdefault('$set', {}).update(updated_at=now())
        update['$inc'] = {'revision': 1}
        document = self.collection.find_one_and_update({'_id': identifier, 'revision': revision, 'is_deleted': False},
                                                     update, return_document=ReturnDocument.AFTER)
        if document is None:
            raise DatasetConflictError('案例已被其他用户修改，请刷新后重试')
        return self.public(document)

    def save(self, payload, identifier=None, revision=None):
        data = validate_case(payload)
        try:
            if identifier:
                # Editing metadata must not clear future plan/model attachments.
                for field in ('linkedPlanIds', 'linkedModelIds'):
                    if field not in payload:
                        data.pop(field)
                return self.change(identifier, revision, {'$set': data})
            timestamp = now()
            document = {'_id': uuid4().hex, **data, 'origin': 'custom', 'revision': 1, 'is_deleted': False,
                        'sort_order': 1000, 'created_at': timestamp, 'updated_at': timestamp,
                        'detail_pages': [], 'active_page_id': '', 'builtin_page_seeded': True}
            self.collection.insert_one(document)
            return self.public(document)
        except DuplicateKeyError:
            raise DatasetConflictError('同名案例已存在，请编辑已有案例或修改名称') from None

    def delete(self, identifier, revision):
        self.change(identifier, revision, {'$set': {'is_deleted': True}})

    def set_links(self, identifier, revision, payload):
        return self.change(identifier, revision, {'$set': validate_links(payload)})

    def attach_page(self, identifier, revision, source, filename, *, origin='upload'):
        current = self.get(identifier)
        if current is None:
            raise KeyError(identifier)
        if current['revision'] != revision:
            raise DatasetConflictError('案例已更新，请刷新后再上传')
        filename = filename.replace('\\', '/').rsplit('/', 1)[-1]
        if not filename.lower().endswith(('.html', '.htm')) or not 1 <= len(filename) <= 180 or any(ord(c) < 32 for c in filename):
            raise ValueError('请选择有效的 .html 或 .htm 文件')
        source.seek(0)
        raw = source.read(MAX_PAGE_BYTES + 1)
        if not raw or len(raw) > MAX_PAGE_BYTES:
            raise ValueError('详情页须为非空 HTML，且不能超过 16 MiB')
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise ValueError('HTML 详情页须使用 UTF-8 编码') from None
        if '\x00' in text or BeautifulSoup(text, 'html.parser').find() is None:
            raise ValueError('文件不是有效的 HTML 页面')
        digest = hashlib.sha256(raw).hexdigest()
        if current.get('detailPage') and current['detailPage']['sha256'] == digest:
            return current
        file_id = self.pages.upload_from_stream(filename, io.BytesIO(raw), metadata={'case_id': identifier, 'content_type': 'text/html'})
        page = {'id': uuid4().hex, 'file_id': file_id, 'filename': filename, 'size': len(raw),
                'sha256': digest, 'uploaded_at': now(), 'origin': origin}
        try:
            return self.change(identifier, revision, {'$push': {'detail_pages': page}, '$set': {'active_page_id': page['id'], 'builtin_page_seeded': True}})
        except Exception:
            self.pages.delete(file_id)
            raise

    def open_page(self, identifier, page_id):
        document = self.collection.find_one({'_id': identifier, 'is_deleted': {'$ne': True}})
        if document is None:
            raise KeyError(identifier)
        page = next((p for p in document.get('detail_pages', []) if p['id'] == page_id), None)
        if page is None:
            raise KeyError(page_id)
        return self.pages.open_download_stream(page['file_id']), page['filename']
