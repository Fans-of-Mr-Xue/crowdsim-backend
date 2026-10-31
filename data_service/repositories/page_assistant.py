"""Assistant threads in Mongo, isolated from independent chat/workbench history."""
from uuid import uuid4
import json
from datetime import datetime, timedelta, timezone
from pymongo import ReturnDocument
from .knowledge_documents import now
from .datasets import DatasetConflictError


class PageAssistantRepository:
    def __init__(self, database):
        self.collection = database['page_assistant_conversations']

    def initialize(self):
        self.collection.create_index([('client_id', 1), ('updated_at', -1)])

    @staticmethod
    def public(doc):
        result = {'id': doc['_id'], **{key: value for key, value in doc.items() if key not in ('_id', 'client_id', 'lease_until', 'turn_id')}}
        if 'messages' in result:
            result['messages'] = [{**{key: value for key, value in message.items() if key != 'plan_json'},
                                   **({'plan': json.loads(message['plan_json'])} if 'plan_json' in message else {})}
                                  for message in result['messages']]
        return result

    @staticmethod
    def title(content, context, plan):
        fields = {row['id']: row.get('value') for row in context['fields']}
        title = plan.get('changes', {}).get('task.title') or fields.get('task.title') or content
        for prefix in ('我想研究', '请帮我研究', '帮我研究', '我想分析', '请帮我'):
            if title.startswith(prefix):
                title = title[len(prefix):].strip()
                break
        return title[:60] or '需求讨论'

    def create_completed(self, client, content, context, plan, model, definition=None):
        timestamp, identifier, turn_id = now(), uuid4().hex, uuid4().hex
        user, assistant = self.messages(turn_id, content, context, plan, model)
        doc = {'_id': identifier, 'client_id': client, 'scope': 'pre-requirements',
               'title': self.title(content, context, plan), 'revision': 1, 'busy': False,
               'created_at': timestamp, 'updated_at': timestamp, 'messages': [user, assistant],
               'task_reference': context.get('task_reference')}
        if definition is not None:
            doc.update(draft_definition=definition, draft_saved_at=timestamp)
        self.collection.insert_one(doc)
        return self.public(doc)

    def delete(self, client, identifier):
        result = self.collection.delete_one({'_id': identifier, 'client_id': client,
            '$or': [{'busy': {'$ne': True}}, {'lease_until': {'$lt': now()}}]})
        if not result.deleted_count and self.collection.find_one({'_id': identifier, 'client_id': client}, {'_id': 1}):
            raise DatasetConflictError('对话正在生成回复，请结束后再删除')
        return {'id': identifier, 'deleted': True}

    def rename(self, client, identifier, title):
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
            raise ValueError('对话标题须为1至100字')
        doc = self.collection.find_one_and_update({'_id': identifier, 'client_id': client, 'busy': False},
            {'$set': {'title': title.strip(), 'custom_title': True}}, return_document=ReturnDocument.AFTER)
        if doc is None:
            raise DatasetConflictError('对话不存在或正在生成回复，请稍后重试')
        return self.public(doc)

    def list(self, client, scope=None):
        if scope not in (None, 'pre-requirements'):
            raise ValueError('不支持的对话范围')
        query = {'client_id': client, 'messages': {'$elemMatch': {'role': 'user', 'content': {'$regex': r'\S'}, 'exampleDemo': {'$ne': True}}}}
        if scope:
            query['$or'] = [{'scope': scope}, {'scope': None, 'messages.page': 'task-definition'}]
        return [self.public(doc) for doc in self.collection.find(query, {'messages': 0, 'draft_definition': 0}).sort('updated_at', -1).limit(50)]

    def draft(self, client, identifier, definition, reference):
        doc = self.collection.find_one_and_update({'_id': identifier, 'client_id': client, 'scope': 'pre-requirements', 'busy': False,
            'messages': {'$elemMatch': {'role': 'user', 'content': {'$regex': r'\S'}, 'exampleDemo': {'$ne': True}}}},
            {'$set': {'draft_definition': definition, 'task_reference': reference, 'draft_saved_at': now(), 'updated_at': now()}}, return_document=ReturnDocument.AFTER)
        if doc is None:
            raise DatasetConflictError('对话正在处理或已切换，请稍后保存会话草稿')
        return self.public(doc)

    def get(self, client, identifier):
        doc = self.collection.find_one({'_id': identifier, 'client_id': client})
        if doc is None:
            raise KeyError(identifier)
        if not doc.get('scope'):
            reference = next((item.get('task_reference') for item in reversed(doc.get('messages', [])) if item.get('page') == 'task-definition'), None)
            if any(item.get('page') == 'task-definition' for item in doc.get('messages', [])):
                update = {'scope': 'pre-requirements', 'task_reference': reference}
                self.collection.update_one({'_id': identifier, 'client_id': client}, {'$set': update})
                doc.update(update)
        return self.public(doc)

    def begin(self, client, identifier, revision, turn_id):
        doc = self.collection.find_one_and_update({'_id': identifier, 'client_id': client, 'revision': revision,
            '$or': [{'busy': False}, {'lease_until': {'$lt': now()}}]},
            {'$set': {'busy': True, 'turn_id': turn_id, 'lease_until': (datetime.now(timezone.utc) + timedelta(minutes=4)).isoformat()}}, return_document=ReturnDocument.AFTER)
        if doc is None:
            raise DatasetConflictError('会话正在处理或已更新，请重新读取会话后发送')
        return self.public(doc)

    @staticmethod
    def messages(turn_id, content, context, plan, model, decision=None):
        timestamp = now()
        reference = context.get('task_reference')
        user = {'id': uuid4().hex, 'role': 'user', 'content': content, 'page': context['page'], 'page_title': context.get('title'), 'task_reference': reference, 'created_at': timestamp}
        if decision:
            # The exact effect remains on the assistant card (serialized JSON).
            # Keep the user receipt compact and free of dotted Mongo field keys.
            user['decision'] = {key: value for key, value in decision.items() if key not in ('changes', 'base_values', 'task_binding')}
        assistant = {'id': turn_id, 'role': 'assistant', 'content': plan['reply'], 'page': context['page'], 'page_title': context.get('title'),
                     'model': model, 'plan_json': json.dumps(plan, ensure_ascii=False), 'execution': None, 'task_reference': reference, 'created_at': timestamp}
        return user, assistant

    def complete(self, client, identifier, turn_id, content, context, plan, model, decision=None):
        timestamp = now()
        user, assistant = self.messages(turn_id, content, context, plan, model, decision)
        doc = self.collection.find_one_and_update({'_id': identifier, 'client_id': client, 'turn_id': turn_id, 'busy': True},
            {'$set': {'busy': False, 'updated_at': timestamp},
             '$inc': {'revision': 1}, '$push': {'messages': {'$each': [user, assistant], '$slice': -200}}}, return_document=ReturnDocument.AFTER)
        if doc is None:
            raise DatasetConflictError('会话处理已过期，请重新读取会话')
        return self.public(doc)

    def release(self, client, identifier, turn_id):
        self.collection.update_one({'_id': identifier, 'client_id': client, 'turn_id': turn_id}, {'$set': {'busy': False}})

    def execution(self, client, identifier, turn_id, result):
        # Write once: reloads must never replay an unacknowledged page mutation.
        updated = self.collection.update_one({'_id': identifier, 'client_id': client,
            'messages': {'$elemMatch': {'id': turn_id, 'role': 'assistant', 'execution': None}}},
            {'$set': {'messages.$.execution': result, 'updated_at': now()}, '$inc': {'revision': 1}})
        if not updated.matched_count:
            raise DatasetConflictError('操作结果已记录或会话不存在，请重新读取')
        return self.get(client, identifier)
