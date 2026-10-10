"""One source for metadata validation and constraints sent to the model."""
from .knowledge_documents import DocumentProfile

TEXT_FIELDS = {"name": 200, "type": 80, "topic": 80, "summary": 4000,
               "trigger_conditions": 12000, "response_level": 4000,
               "responsible_departments": 8000, "core_actions": 16000,
               "scope": 4000, "publisher": 500, "published_at": 100}
FIELD_LABELS = {"name": "预案名称", "type": "预案类型", "topic": "所属专题", "summary": "摘要",
                "trigger_conditions": "启动条件", "response_level": "响应等级",
                "responsible_departments": "责任部门与职责", "core_actions": "核心处置动作与流程",
                "scope": "适用范围", "publisher": "发布机构", "published_at": "发布日期"}
MAX_TAGS = 30
MAX_TAG_CHARS = 80
PROFILE = DocumentProfile("emergency_plans", "emergency-plans", "应急预案", "emergency_plan", TEXT_FIELDS, FIELD_LABELS,
                          {"summary": 1500, "trigger_conditions": 2500, "response_level": 1200, "responsible_departments": 2200, "core_actions": 1000}, max_tags=MAX_TAGS, max_tag_chars=MAX_TAG_CHARS)


def prompt_constraints():
    return PROFILE.constraints()


def metadata_example():
    return PROFILE.example()


def validate_metadata(payload, *, require_name=True):
    return PROFILE.validate(payload, require_name=require_name)
