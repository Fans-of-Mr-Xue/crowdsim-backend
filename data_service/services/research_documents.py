"""Require source quotations before automatically constructing plans or regulations."""
import re


def separate_regulations(metadata):
    """Regulatory knowledge stays in its own library instead of duplicating a plan."""
    types = list(metadata.get("knowledge_types", []))
    legal_source = re.search(r"法律|法规|规章|标准|规范性文件|政策解读|政策文件", metadata.get("type", ""))
    if "regulation" in types or legal_source:
        metadata["knowledge_types"] = [value for value in types if value != "emergency_plan"]
        metadata["plan_evidence"] = ""
    return metadata


def check_source_evidence(metadata, source):
    compact_source = re.sub(r"\s+", "", source)
    types = list(metadata.get("knowledge_types", []))
    for knowledge_type, field in (("emergency_plan", "plan_evidence"), ("regulation", "regulation_evidence")):
        evidence = metadata.get(field, "").strip(' \n\t“”"')
        compact_evidence = re.sub(r"\s+", "", evidence)
        if knowledge_type not in types or len(compact_evidence) < 12 or compact_evidence not in compact_source:
            if knowledge_type in types:
                types.remove(knowledge_type)
            metadata[field] = ""
        else:
            metadata[field] = evidence
    metadata["knowledge_types"] = types
    return separate_regulations(metadata)
