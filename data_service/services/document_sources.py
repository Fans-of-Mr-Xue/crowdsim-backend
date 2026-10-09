"""Extract readable source text without sending whole files to the model."""
import asyncio
from contextlib import suppress
from bs4 import BeautifulSoup
from pypdf import PdfReader
from pypdf.errors import PdfReadError

CHUNK_CHARS = 10000


def validate_pdf(source):
    source.seek(0)
    if not source.read(1024).lstrip().startswith(b"%PDF-"):
        raise ValueError("请选择有效的 PDF 文件")
    source.seek(0)
    try:
        reader = PdfReader(source)
        if reader.is_encrypted:
            raise ValueError("请先解除 PDF 密码保护后再导入")
        if not len(reader.pages):
            raise ValueError("PDF 中没有页面")
    except PdfReadError:
        raise ValueError("PDF 文件无法读取，请检查文件是否损坏") from None
    finally:
        source.seek(0)


def text_chunks(source, information):
    reader = PdfReader(source)
    information.update(page_count=len(reader.pages), textless_page_count=0, textless_pages=[])
    buffer = ""
    for index, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if not text:
            information["textless_page_count"] += 1
            if len(information["textless_pages"]) < 100:
                information["textless_pages"].append(index)
            continue
        buffer += f"\n[第{index}页]\n{text}\n"
        while len(buffer) >= CHUNK_CHARS:
            yield buffer[:CHUNK_CHARS]
            buffer = buffer[CHUNK_CHARS:]
    if buffer.strip():
        yield buffer


def next_chunk(iterator):
    return next(iterator, None)


async def file_io(function, *args):
    # A cancelled request must not close a temporary file while a thread uses it.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with suppress(Exception):
            await task
        raise



def html_text(source):
    source.seek(0)
    soup = BeautifulSoup(source.read(), "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else "网页资料"
    for tag in soup.select("script,style,noscript,nav,footer,header,form,svg,iframe"):
        tag.decompose()
    # Some government pages include several nested HTML/body fragments from headers.
    # Select the substantive article block rather than the first body/navigation fragment.
    candidates = soup.select("#UCAP-CONTENT, .TRS_Editor, .pages_content, article, main, [role=main], #article-content, .article-content, .article_content, .post-content, .entry-content")
    body = max(candidates, key=lambda tag: len(tag.get_text(" ", strip=True))) if candidates else soup
    lines = [line.strip() for line in body.get_text("\n", strip=True).splitlines() if line.strip()]
    if len("\n".join(lines)) < 40:
        raise ValueError("网页没有可提取的正文，可能需要登录或依赖脚本加载")
    metadata = []
    for tag in soup.select("meta[name], meta[property]"):
        key = (tag.get("name") or tag.get("property") or "").lower()
        if any(part in key for part in ("articletitle", "pubdate", "contentsource", "published_time", "author")) and tag.get("content"):
            metadata.append(f"{key}：{tag['content']}")
    return title[:200], "\n".join([f"网页标题：{title}", *metadata, *lines])


def source_chunks(source, information, source_kind="pdf"):
    if source_kind == "pdf":
        yield from text_chunks(source, information)
    else:
        _, text = html_text(source)
        information.update(page_count=0, textless_page_count=0, textless_pages=[])
        for offset in range(0, len(text), CHUNK_CHARS):
            yield text[offset:offset + CHUNK_CHARS]
