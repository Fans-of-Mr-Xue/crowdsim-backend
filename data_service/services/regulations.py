"""Keep regulation dates and current-effect claims anchored to original text."""
import re


def check_source_evidence(metadata, source):
    text = re.sub(r"\s+", "", source)
    status = metadata.get("legal_status", "")
    # A publication date or lack of a repeal notice cannot establish current validity.
    if re.search(r"现行有效|目前有效|仍然有效|仍有效", status) and not any(phrase in text for phrase in ("现行有效", "目前有效", "仍然有效", "仍有效")):
        metadata["legal_status"] = ""
    date = metadata.get("effective_at", "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        year, month, day = date.split("-")
        pattern = rf"{year}年0?{int(month)}月0?{int(day)}日|{re.escape(date)}"
        has_evidence = any(re.search(r"施行|实施|生效|执行", text[max(0, match.start() - 25):match.end() + 25]) for match in re.finditer(pattern, text))
        if not has_evidence:
            metadata["effective_at"] = ""
    return metadata
