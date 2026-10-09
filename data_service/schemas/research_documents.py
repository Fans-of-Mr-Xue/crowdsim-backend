"""Original-resource metadata and evidence-backed knowledge classification."""
from .knowledge_documents import DocumentProfile

KNOWLEDGE_TYPES = ("emergency_plan", "regulation", "model", "method")
TEXT_FIELDS = {"name": 200, "type": 80, "topic": 80, "summary": 4000,
               "authors": 1000, "publisher": 500, "published_at": 100, "scope": 4000,
               "research_object": 4000, "methods": 4000, "key_findings": 6000,
               "application_value": 4000, "limitations": 4000,
               "plan_evidence": 600, "regulation_evidence": 600}
FIELD_LABELS = {"name": "资料名称", "type": "资料类型", "topic": "所属专题", "summary": "摘要",
                "authors": "作者或编制单位", "publisher": "发布机构", "published_at": "发布日期",
                "scope": "资料涉及区域与范围", "research_object": "研究或讨论对象", "methods": "模型与方法",
                "key_findings": "主要内容与结论", "application_value": "可用于仿真的知识", "limitations": "适用条件与局限",
                "plan_evidence": "预案判定的原文依据", "regulation_evidence": "法规标准判定的原文依据",
                "knowledge_types": "可构建的知识类型"}
PROFILE = DocumentProfile("research_documents", "research-documents", "研究文献", "research_document", TEXT_FIELDS, FIELD_LABELS,
                          {"summary": 1200, "scope": 1200, "research_object": 1000, "methods": 1500,
                           "key_findings": 1800, "application_value": 1200, "limitations": 1000},
                          list_fields={"knowledge_types": KNOWLEDGE_TYPES})
