"""Fetch public HTTP(S) sources with streaming PDF transfers and bounded HTML snapshots."""
import hashlib
import ipaddress
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
from aiohttp.resolver import DefaultResolver

from ..services.document_sources import file_io, html_text, validate_pdf


def validate_source_url(url):
    if not isinstance(url, str) or len(url) > 4000 or any(ord(char) < 33 for char in url):
        raise ValueError("请输入完整且有效的 HTTP(S) 链接")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("仅支持不含登录凭证的 HTTP(S) 链接")
    try:
        if not ipaddress.ip_address(parts.hostname).is_global:
            raise ValueError("仅支持公开网站，不允许访问本机或内网地址")
    except ValueError as error:
        if "仅支持" in str(error):
            raise
    return url


class PublicResolver(DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        addresses = await super().resolve(host, port, family)
        if not addresses or any(not ipaddress.ip_address(address["host"]).is_global for address in addresses):
            raise ValueError("链接解析到本机或内网地址，无法导入")
        return addresses


@dataclass
class WebDocument:
    filename: str
    kind: str
    size: int
    checksum: str
    url: str
    title: str = ""


async def fetch_document(url, destination):
    url = validate_source_url(url)
    connector = aiohttp.TCPConnector(resolver=PublicResolver())
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=180), trust_env=False,
                                        headers={"User-Agent": "CrowdSim-DataService/1.0"}) as session:
            for _ in range(6):
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise ValueError("网站重定向缺少目标链接")
                        url = validate_source_url(urljoin(url, location))
                        continue
                    if response.status != 200:
                        raise ValueError(f"在线资料返回 HTTP {response.status}，请确认链接公开可访问")
                    digest, size, prefix = hashlib.sha256(), 0, b""
                    mime = response.content_type
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        if len(prefix) < 1024:
                            prefix = (prefix + chunk)[:1024].lstrip()
                        size += len(chunk)
                        if not prefix.startswith(b"%PDF-") and size > 10 * 1024 * 1024:
                            raise ValueError("网页内容超过 10 MB，请直接导入原始 PDF")
                        digest.update(chunk)
                        await file_io(destination.write, chunk)
                    if not size:
                        raise ValueError("链接返回空文件")
                    await file_io(destination.seek, 0)
                    if prefix.startswith(b"%PDF-"):
                        await file_io(validate_pdf, destination)
                        filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or "在线资料.pdf"
                        if not filename.lower().endswith(".pdf"):
                            filename += ".pdf"
                        filename = "".join(char for char in filename if ord(char) >= 32 and char not in "/\\")[:175]
                        return WebDocument(filename, "pdf", size, digest.hexdigest(), url)
                    if mime not in {"text/html", "application/xhtml+xml"} and not prefix.lower().startswith((b"<!doctype html", b"<html")):
                        raise ValueError("链接必须指向 PDF 文件或可读取正文的网页")
                    title, text = await file_io(html_text, destination)
                    if len(text.strip()) < 40:
                        raise ValueError("网页没有可提取的正文，可能需要登录或依赖脚本加载")
                    await file_io(destination.seek, 0)
                    return WebDocument("网页资料.html", "webpage", size, digest.hexdigest(), url, title)
            raise ValueError("在线资料重定向次数过多")
    except (aiohttp.ClientError, TimeoutError):
        raise ValueError("无法读取在线资料，请检查网络、链接及网站访问权限") from None
