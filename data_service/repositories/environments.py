"""Persistent environment profiles; large road networks and media use GridFS."""
import io
import json
from pathlib import Path
from uuid import uuid4
from gridfs import GridFSBucket, NoFile
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError
from .datasets import DatasetConflictError
from .knowledge_documents import now
from ..schemas.environments import validate_profile, leaflet_polygon

RESOURCES = Path(__file__).resolve().parents[1] / 'resources'


class EnvironmentRepository:
    def __init__(self, database):
        self.database = database
        self.collection = database['environment_profiles']
        self.assets = GridFSBucket(database, bucket_name='environment_assets')

    def initialize(self):
        self.collection.create_index([('is_deleted', 1), ('sort_order', 1)])
        self.collection.create_index('title', unique=True, partialFilterExpression={'is_deleted': False})
        template = json.loads((RESOURCES / 'environment_defaults.json').read_text())
        self.database['environment_settings'].update_one({'_id': 'manual_region_template'}, {'$setOnInsert': {'value': template}}, upsert=True)
        seed_roads = json.loads((RESOURCES / 'environment_roads.json').read_text())
        for index, seed in enumerate(json.loads((RESOURCES / 'environment_profiles.json').read_text())):
            identifier = seed.pop('id')
            if self.collection.find_one({'_id': identifier}):
                continue  # Seeds never overwrite edits or restore user deletions.
            data = validate_profile(seed)
            timestamp = now()
            road_file = None
            roads = seed_roads.get(identifier)
            if roads:
                road_file = self.assets.upload_from_stream(identifier + '-roads.geojson', io.BytesIO(json.dumps(roads, ensure_ascii=False).encode()))
                data.update(roads_file_id=road_file, roadsCount=len(roads['features']), roadsStatus='available', roadsMessage=f"已获取 {len(roads['features'])} 条区域及周边道路 / 步行通道")
            try:
                result = self.collection.update_one({'_id': identifier}, {'$setOnInsert': {**data, 'revision': 1, 'is_deleted': False, 'created_at': timestamp, 'updated_at': timestamp, 'sort_order': index}}, upsert=True)
            except Exception:
                if road_file:
                    self.assets.delete(road_file)
                raise
            if road_file and result.upserted_id is None:
                self.assets.delete(road_file)
        milan = self.collection.find_one({'_id': 'milan-portrait-region'})
        if milan and not milan.get('density_media_id'):
            with (RESOURCES / 'milan_density.gif').open('rb') as source:
                media_id = self.assets.upload_from_stream('milan_density.gif', source, metadata={'content_type': 'image/gif'})
            result = self.collection.update_one({'_id': milan['_id'], 'density_media_id': {'$exists': False}}, {'$set': {'density_media_id': media_id}})
            if not result.modified_count:
                self.assets.delete(media_id)

    @staticmethod
    def public(doc):
        if doc is None:
            return None
        data = {key: value for key, value in doc.items() if key not in ('_id', 'roads_file_id', 'density_media_id', 'is_deleted', 'sort_order')}
        return {'id': doc['_id'], **data, 'polygon': leaflet_polygon(doc['geometry']), 'roadsCount': doc.get('roadsCount', 0)}

    def list(self, search=''):
        query = {'is_deleted': {'$ne': True}}
        if search:
            import re
            query['title'] = {'$regex': re.escape(search), '$options': 'i'}
        docs = list(self.collection.find(query).sort([('sort_order', 1), ('created_at', -1)]).limit(1000))
        stats = {'total': len(docs), 'ready': sum(d.get('status') == 'ready' for d in docs), 'collecting': sum(d.get('status') != 'ready' for d in docs)}
        template = self.database['environment_settings'].find_one({'_id': 'manual_region_template'})
        return {'items': [self.public(d) for d in docs], 'stats': stats, 'customRegionTemplate': template['value'] if template else {}}

    def get(self, identifier):
        return self.public(self.collection.find_one({'_id': identifier, 'is_deleted': {'$ne': True}}))

    def save(self, payload, identifier=None, revision=None):
        data = validate_profile(payload)
        existing = None
        if identifier:
            existing = self.collection.find_one({'_id': identifier, 'is_deleted': {'$ne': True}})
            if existing is None:
                raise KeyError(identifier)
            if existing['revision'] != revision:
                raise DatasetConflictError('区域画像已被其他用户修改，请刷新后重试')
        identifier = identifier or uuid4().hex
        data['cityCode'] = data.get('cityCode') or 'REG-' + identifier[:8].upper()
        roads = data.pop('roads', None)
        new_file_id = None
        geometry_changed = existing is not None and existing['geometry'] != data['geometry']
        if roads is not None:
            raw = json.dumps(roads, ensure_ascii=False).encode()
            new_file_id = self.assets.upload_from_stream(identifier + '-roads.geojson', io.BytesIO(raw))
            data.update(roads_file_id=new_file_id, roadsCount=len(roads['features']), roadsStatus='available' if roads['features'] else 'unavailable')
        elif geometry_changed:
            data.update(roadsCount=0, roadsStatus='unavailable', roadsMessage='边界已修改，可重新获取对应路网')
        data.update(updated_at=now())
        try:
            if existing:
                update = {'$set': data, '$inc': {'revision': 1}}
                if geometry_changed and roads is None:
                    update['$unset'] = {'roads_file_id': ''}
                doc = self.collection.find_one_and_update({'_id': identifier, 'revision': revision, 'is_deleted': {'$ne': True}}, update, return_document=ReturnDocument.AFTER)
                if doc is None:
                    raise DatasetConflictError('区域画像正在更新，请刷新后重试')
            else:
                doc = {'_id': identifier, **data, 'revision': 1, 'is_deleted': False, 'created_at': now(), 'sort_order': 1000}
                self.collection.insert_one(doc)
        except Exception as exc:
            if new_file_id:
                self.assets.delete(new_file_id)
            if isinstance(exc, DuplicateKeyError):
                raise DatasetConflictError('同名区域画像已存在，请修改名称或编辑已有区域') from None
            raise
        if existing and existing.get('roads_file_id') and (roads is not None or geometry_changed):
            try:
                self.assets.delete(existing['roads_file_id'])
            except NoFile:
                pass
        return self.public(doc)

    def delete(self, identifier, revision):
        result = self.collection.update_one({'_id': identifier, 'revision': revision, 'is_deleted': {'$ne': True}}, {'$set': {'is_deleted': True, 'updated_at': now()}, '$inc': {'revision': 1}})
        if not result.matched_count:
            raise DatasetConflictError('区域画像已更新或删除，请刷新后重试')

    def open_roads(self, identifier):
        doc = self.collection.find_one({'_id': identifier, 'is_deleted': {'$ne': True}})
        if doc is None:
            raise KeyError(identifier)
        if not doc.get('roads_file_id'):
            return {'type': 'FeatureCollection', 'features': []}
        with self.assets.open_download_stream(doc['roads_file_id']) as stream:
            return json.load(stream)

    def open_media(self, identifier):
        doc = self.collection.find_one({'_id': identifier, 'is_deleted': {'$ne': True}})
        if doc is None or not doc.get('density_media_id'):
            raise KeyError(identifier)
        return self.assets.open_download_stream(doc['density_media_id'])
