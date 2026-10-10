"""Emergency-plan compatibility entry points over shared document parsing."""
from .document_sources import file_io, validate_pdf, text_chunks
from .knowledge_documents import DocumentParser, extraction_prompt as build_prompt, model_metadata as normalize_metadata
from ..schemas.emergency_plans import PROFILE


def extraction_prompt(kind="extract", max_output_tokens=3000):
    return build_prompt(PROFILE, kind, max_output_tokens)


def model_metadata(value):
    return normalize_metadata(value, PROFILE)


class PlanParser(DocumentParser):
    pass
