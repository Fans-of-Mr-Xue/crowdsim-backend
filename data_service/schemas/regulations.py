"""Regulation-specific metadata; no emergency response fields are imposed on laws."""
from .knowledge_documents import DocumentProfile

TEXT_FIELDS = {"name": 200, "type": 80, "topic": 80, "summary": 4000, "document_number": 200,
               "legal_level": 100, "effective_at": 100, "legal_status": 300, "scope": 4000,
               "publisher": 500, "published_at": 100, "responsible_departments": 8000,
               "key_provisions": 12000, "mandatory_requirements": 12000}
FIELD_LABELS = {"name": "法规名称", "type": "法规类型", "topic": "所属专题", "summary": "摘要",
                "document_number": "文号", "legal_level": "效力层级", "effective_at": "施行日期",
                "legal_status": "原文注明的效力状态", "scope": "适用范围", "publisher": "发布机构",
                "published_at": "发布日期", "responsible_departments": "责任主体与职责",
                "key_provisions": "关键条款", "mandatory_requirements": "强制要求"}
PROFILE = DocumentProfile("regulations", "regulations", "法规", "regulation", TEXT_FIELDS, FIELD_LABELS,
                          {"summary": 1500, "scope": 1500, "responsible_departments": 2200, "key_provisions": 1800, "mandatory_requirements": 1800})
