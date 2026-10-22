"""Bounded public-source collection for PostAgent; model output is never a source."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import re
from tempfile import SpooledTemporaryFile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def _search(query):
    # Optional Tavily-compatible provider. Credentials stay on the backend.
    key = os.environ.get("CROWDSIM_POST_SEARCH_API_KEY")
    if not key:
        return [], "未配置联网搜索服务；可直接提供公开网页或 PDF 链接进行读取。"
    endpoint = os.environ.get("CROWDSIM_POST_SEARCH_URL", "https://api.tavily.com/search")
    if urlsplit(endpoint).scheme != "https":
        raise ValueError("联网搜索服务必须使用 HTTPS")
    request = Request(endpoint, data=json.dumps({"query": query[:1000], "max_results": 3,
        "search_depth": "basic", "include_answer": False, "include_raw_content": False}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urlopen(request, timeout=25) as response:
            data = json.loads(response.read(1024 * 1024))
        return [row["url"] for row in data.get("results", [])[:3] if isinstance(row, dict) and isinstance(row.get("url"), str)], ""
    except Exception:
        return [], "联网搜索服务未返回可用结果，请重试或提供公开链接。"


async def _read_sources(urls):
    from data_service.connectors.web_sources import fetch_document
    from data_service.services.document_sources import html_text
    sources, errors = [], []
    for url in urls[:3]:
        try:
            with SpooledTemporaryFile(max_size=1024 * 1024) as handle:
                document = await asyncio.wait_for(fetch_document(url, handle), timeout=25)
                if document.size > 10 * 1024 * 1024:
                    raise ValueError("资料超过 10 MiB，请上传节选")
                if document.kind == "pdf":
                    from pypdf import PdfReader
                    reader = PdfReader(handle)
                    body = "\n".join(page.extract_text() or "" for page in reader.pages[:12])
                else:
                    _, body = html_text(handle)
                if not body.strip():
                    raise ValueError("正文无法提取")
                sources.append({"title": document.title or document.filename, "url": document.url,
                    "checksum": document.checksum, "retrievedAt": datetime.now(timezone.utc).isoformat(), "excerpt": body[:10000]})
        except Exception as error:
            # Do not expose provider credentials, response bodies or network internals.
            errors.append({"url": url, "message": str(error) if isinstance(error, ValueError) else "资料读取失败，请确认链接公开可访问。"})
    return sources, errors


def collect_sources(query):
    urls = list(dict.fromkeys(re.findall(r'https?://[^\s<>"\u3000，。；）)]+', query)))[:3]
    notice = ""
    if not urls:
        urls, notice = _search(query)
    sources, errors = asyncio.run(_read_sources(urls)) if urls else ([], [])
    return {"sources": sources, "errors": errors, "notice": notice,
            "status": "collected" if sources else "needs_sources"}
