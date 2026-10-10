"""Persistent, leased background parsing queue owned by the data process."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import tempfile
from uuid import uuid4

from pypdf.errors import PdfReadError
from pymongo.errors import PyMongoError

from .document_sources import file_io, next_chunk, source_chunks
from .pdf_images import extract_region_image
from .regulations import check_source_evidence
from ..connectors.llm import ModelError
from ..prompts import load_prompt

logger = logging.getLogger(__name__)


def extraction_prompt(profile, kind="extract", max_output_tokens=3000):
    prompt = load_prompt(profile.prompt_prefix + "_extract").replace("{{field_constraints}}", profile.constraints()).replace("{{metadata_example}}", profile.example()).replace("{{max_output_tokens}}", str(max_output_tokens))
    if kind != "extract":
        prompt += "\n\n" + load_prompt(profile.prompt_prefix + "_" + kind)
    return prompt


class ParseAbandoned(Exception):
    """The plan was deleted or another worker owns its lease."""


def model_metadata(value, profile):
    """Models sometimes return structured lists for departments/actions; retain them as text."""
    def text(item):
        if isinstance(item, str):
            return item
        if isinstance(item, list):
            return "\n".join(text(part) for part in item)
        if isinstance(item, dict):
            return "；".join(f"{key}：{text(part)}" for key, part in item.items())
        if item is None:
            return ""
        return str(item)

    if not isinstance(value, dict):
        raise ValueError("模型返回的资料元数据必须是对象")
    normalized = dict(value)
    for key in profile.fields:
        if key in normalized:
            normalized[key] = text(normalized[key])
    if isinstance(normalized.get("tags"), str):
        normalized["tags"] = [normalized["tags"]]
    return profile.validate(normalized, require_name=False, for_model=True)


class DocumentParser:
    def __init__(self, repository, model):
        self.repository = repository
        self.model = model
        self.profile = repository.profile

    async def extract_metadata(self, source, kind="extract"):
        budget = getattr(self.model, "max_output_tokens", 3000)
        value = await self.model.extract(extraction_prompt(self.profile, kind, budget), source)
        try:
            metadata = model_metadata(value, self.profile)
        except ValueError as exc:
            # Ask the model to satisfy the same constraints, rather than silently clipping facts.
            repaired = await self.model.extract(extraction_prompt(self.profile, "repair", budget), json.dumps({"validation_error": str(exc), "metadata": value}, ensure_ascii=False))
            try:
                metadata = model_metadata(repaired, self.profile)
            except ValueError:
                raise ModelError("模型返回的资料字段仍不符合要求，请重新解析或手工补充元数据；原始资料已保存") from None
        if kind == "extract" and self.profile.prompt_prefix == "regulation":
            metadata = check_source_evidence(metadata, source)
        return metadata

    async def heartbeat(self, identifier, run_id):
        while True:
            await asyncio.sleep(30)
            if not await asyncio.to_thread(self.repository.heartbeat, identifier, run_id):
                return

    async def parse(self, doc, run_id):
        identifier = doc["_id"]
        information = {"model": self.model.model, "chunk_count": 0}
        # Original PDFs stay in GridFS; temporary local copies are always removed.
        with tempfile.NamedTemporaryFile(suffix=".pdf") as source:
            await file_io(self.repository.copy_file, identifier, source)
            source_kind = doc.get("source_kind", "pdf")
            if source_kind == "pdf":
                try:
                    image = await file_io(extract_region_image, source.name)
                    if not await file_io(self.repository.save_image, identifier, run_id, image):
                        raise ParseAbandoned
                except ParseAbandoned:
                    raise
                except (ValueError, RuntimeError):
                    logger.warning("Region image could not be extracted from %s", identifier, exc_info=True)
                    information["image_warning"] = "区域图提取未完成，请查看原始资料"
            source.seek(0)
            iterator = source_chunks(source, information, source_kind)
            merged = None
            batch = []
            verified_dates = set()
            verified_statuses = []
            while (chunk := await file_io(next_chunk, iterator)) is not None:
                value = await self.extract_metadata(chunk)
                if self.profile.prompt_prefix == "regulation":
                    if value.get("effective_at"):
                        verified_dates.add(value["effective_at"])
                    if value.get("legal_status"):
                        verified_statuses.append(value["legal_status"])
                batch.append(value)
                information["chunk_count"] += 1
                if not await asyncio.to_thread(self.repository.progress, identifier, run_id, information["chunk_count"], 0):
                    raise ParseAbandoned
                # Hierarchical merging bounds context size without discarding later pages.
                if len(batch) == 4:
                    inputs = ([merged] if merged else []) + batch
                    merged = await self.extract_metadata(json.dumps(inputs, ensure_ascii=False), "merge")
                    batch = []
            if not information["chunk_count"]:
                raise ValueError("资料没有可提取的正文；PDF 可能是扫描件，网页可能需要登录或由脚本加载。原始资料已保存，可手工填写元数据或对扫描件进行 OCR")
            inputs = ([merged] if merged else []) + batch
            metadata = inputs[0] if len(inputs) == 1 else await self.extract_metadata(json.dumps(inputs, ensure_ascii=False), "merge")
            metadata["name"] = metadata["name"] or doc["name"]
            metadata["type"] = metadata["type"] or self.profile.label
            metadata["topic"] = metadata["topic"] or "其他"
            if self.profile.prompt_prefix == "regulation":
                # Merging fragments must not reintroduce dates/current-validity claims rejected earlier.
                if metadata.get("effective_at") not in verified_dates:
                    metadata["effective_at"] = ""
                metadata.update(check_source_evidence({"legal_status": metadata.get("legal_status", "")}, "\n".join(verified_statuses)))
            substantive = ("summary", "scope", "responsible_departments", "core_actions", "key_provisions", "mandatory_requirements", "trigger_conditions")
            if not any(metadata.get(field) for field in substantive):
                raise ModelError("未提取到有效的正文元数据，请确认资料包含正文后重新解析；原始资料已保存")
            await asyncio.to_thread(self.repository.progress, identifier, run_id, information["chunk_count"], information["chunk_count"])
            await asyncio.to_thread(self.repository.finish, identifier, run_id, metadata=metadata, details=information)

    async def run(self):
        while True:
            identifier = None
            heartbeat = None
            run_id = uuid4().hex
            try:
                doc = await asyncio.to_thread(self.repository.claim, run_id)
                if doc is None:
                    await asyncio.sleep(1)
                    continue
                identifier = doc["_id"]
                heartbeat = asyncio.create_task(self.heartbeat(identifier, run_id))
                try:
                    await self.parse(doc, run_id)
                except ParseAbandoned:
                    pass
                except (ModelError, ValueError, PdfReadError) as exc:
                    await asyncio.to_thread(self.repository.finish, identifier, run_id, error=str(exc)[:1000])
                except Exception:
                    logger.exception("Knowledge document parsing failed: %s", identifier)
                    await asyncio.to_thread(self.repository.finish, identifier, run_id, error="解析未完成，请检查数据服务日志后重试；原始资料已保留")
            except asyncio.CancelledError:
                if identifier:
                    with suppress(PyMongoError):
                        await asyncio.to_thread(self.repository.release, identifier, run_id)
                raise
            except PyMongoError:
                logger.warning("Knowledge document queue waiting for MongoDB")
                await asyncio.sleep(3)
            finally:
                if heartbeat:
                    heartbeat.cancel()
                    with suppress(asyncio.CancelledError, PyMongoError):
                        await heartbeat
