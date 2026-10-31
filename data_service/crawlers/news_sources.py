"""Public web discovery and verified article extraction, without model-generated news."""
import asyncio
import base64
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit, urljoin, parse_qs, urlunsplit
from urllib.robotparser import RobotFileParser

import aiohttp
from bs4 import BeautifulSoup

from ..connectors.web_sources import PublicResolver, validate_source_url
from ..schemas.news import CHINA_TIME, normalize, parse_date
from .locations import article_locations

AGENT = "CrowdSim-NewsCrawler/1.0"
MAX_BYTES = 4 * 1024 * 1024


def canonical_url(url):
    parts = urlsplit(validate_source_url(url))
    query = [(k, v) for k, values in parse_qs(parts.query).items() for v in values
             if not k.lower().startswith("utm_") and k.lower() not in {"spm", "from", "ref"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(sorted(query)), ""))


def parse_published(value):
    if not value:
        return None
    value = str(value).strip()
    try:
        if re.fullmatch(r"\d{14}", value):
            value = datetime.strptime(value, "%Y%m%d%H%M%S").isoformat()
        elif re.search(r"\d{4}年", value):
            value = value.replace("年", "-").replace("月", "-").replace("日", " ")
        found = re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[T\s]+\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?", value)
        if found:
            date = found[0].replace("/", "-")
            date = re.sub(r"\s+", "T", date)
            date = re.sub(r"(?<=T)\d(?=:)", lambda m: "0" + m[0], date)
            # ISO parser requires zero-padded month/day/hour.
            date = re.sub(r"(?<=-)\d(?=-|T|\s|$)", lambda m: "0" + m[0], date)
            return parse_date(date)
        parsed = parsedate_to_datetime(value)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=CHINA_TIME)).astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def search_links(html, base):
    soup = BeautifulSoup(html, "html.parser")
    if "安全验证" in str(soup.title) or "验证码" in str(soup.title):
        raise ValueError("搜索网站要求验证码")
    links = []
    for anchor in soup.select(".b_algo h2 a, .news-card .title, h3 a, .news-title a"):
        url = urljoin(base, anchor.get("href", ""))
        if "bing.com/ck/" in url:
            encoded = parse_qs(urlsplit(url).query).get("u", [""])[0]
            if encoded.startswith("a1"):
                try:
                    raw = encoded[2:]
                    url = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
                except (ValueError, UnicodeError):
                    continue
        try:
            links.append({"url": canonical_url(url)})
        except ValueError:
            continue
    if not links:
        raise ValueError("搜索未返回可读取的新闻链接")
    return links


def extract_article(html, url, task, candidate=None):
    soup = BeautifulSoup(html, "html.parser")
    def meta(*names):
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return ""
    title = meta("og:title", "twitter:title")
    if not title:
        heading = soup.select_one("h1") or soup.title
        title = heading.get_text(" ", strip=True) if heading else ""
        title = re.split(r"[_|]", title)[0].strip()
    if not title or any(term in title for term in ("安全验证", "验证码", "Access Denied", "页面不存在")):
        raise ValueError("页面不是可读取的新闻正文")
    published = parse_published(meta("article:published_time", "datePublished", "pubdate", "publishdate", "publishTime", "PubDate", "date", "DC.date.issued"))
    source = meta("source", "og:site_name", "publisher")
    if not source:
        publisher = soup.select_one('#source_baidu')
        if publisher:
            source = re.sub(r'^来源\s*[:：]\s*', '', publisher.get_text(' ', strip=True)).strip()
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
            entries = data if isinstance(data, list) else [data]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                for node in [entry, *entry.get("@graph", [])]:
                    if not isinstance(node, dict):
                        continue
                    if "Article" in str(node.get("@type", "")):
                        published = published or parse_published(node.get("datePublished"))
                        publisher = node.get("publisher", {})
                        if isinstance(publisher, dict):
                            source = source or publisher.get("name", "")
        except (ValueError, TypeError):
            pass
    if not published:
        match = re.search(r'(?:publishDate|pubtime|publishTime)\s*[:=]\s*[\'"]([^\'"]+)', html, re.I)
        if match:
            published = parse_published(match[1])
            # Old CNTV templates use Unix epoch as an unset script timestamp.
            # Read the actual visible publication time instead of rejecting it.
            if published and published.timestamp() == 0:
                published = None
    if not published:
        for node in soup.select("time, .info, .time, .pubtime, .pub_time, .publish-time, #pubtime, #pubtime_baidu, .left-time, .source, .source-time"):
            published = parse_published(node.get("datetime") or node.get_text(" ", strip=True))
            if published:
                break
    published = published or parse_published((candidate or {}).get("published"))
    if not published:
        raise ValueError("无法确认新闻发布时间")
    if not parse_date(task["timeRange"][0]) <= published <= parse_date(task["timeRange"][1]):
        raise ValueError("新闻发布时间不在任务时间窗内")
    for node in soup.select("script, style, nav, header, footer, aside, .related, .recommend, .advertisement"):
        node.decompose()
    body = soup.select_one(".left_zw") or soup.select_one("article, #content_area, #p-detail, .article-content, .article_content, #articleContent, #article_content, .TRS_Editor, .article, #content, .content")
    paragraphs = (body or soup).find_all("p")
    content = "\n".join(p.get_text(" ", strip=True) for p in paragraphs if len(p.get_text(strip=True)) >= 20)
    if len(content) < 120:
        raise ValueError("新闻正文不足 120 字，可能为列表、视频或动态页面")
    text = normalize(title + " " + content)
    # Both the geographic target and a configured topic must occur in the actual article.
    if normalize(task["regionKeyword"]) not in text:
        raise ValueError("新闻未匹配目标地区")
    topics = [normalize(k) for k in task["keywords"] if normalize(k) != normalize(task["regionKeyword"])]
    if not topics or not any(keyword in text for keyword in topics):
        raise ValueError("新闻未匹配事件主题关键词")
    category = "事件报道"
    for name, words in (("调查与复盘", ("调查报告", "复盘", "调查组")), ("抢险救援", ("救援", "抢险", "转移")),
                        ("交通与地下空间", ("地铁", "隧道", "停运")), ("极端天气", ("降雨", "暴雨", "气象")),
                        ("城市洪涝", ("内涝", "洪水", "洪涝"))):
        if any(word in title for word in words):
            category = name
            break
    return {"title": title[:500], "url": canonical_url(url), "source": str(source or urlsplit(url).hostname)[:200],
            "publishedAt": published.isoformat(), "date": published.astimezone(CHINA_TIME).strftime("%Y-%m-%d %H:%M"),
            "content": content[:200000], "summary": content[:260], "category": category,
            "location": task["location"], "verified": True,
            **article_locations(title[:500], content[:200000], task["regionId"])}


class NewsSources:
    def __init__(self):
        self.session = None
        self.locks = {}
        self.robots = {}

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicResolver(), limit=12),
            timeout=aiohttp.ClientTimeout(total=25), headers={"User-Agent": AGENT}, trust_env=False)
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    async def request(self, url):
        url = validate_source_url(url)
        for _ in range(6):
            async with self.session.get(url, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    url = validate_source_url(urljoin(url, response.headers.get("Location", "")))
                    continue
                if response.status != 200:
                    raise ValueError(f"网站返回 HTTP {response.status}")
                data = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError("网页超过 4 MiB")
                charset = response.charset
                if not charset:
                    found = re.search(br'charset\s*=\s*[\'"]?([\w-]+)', bytes(data[:4096]), re.I)
                    charset = found[1].decode("ascii") if found else "utf-8"
                try:
                    text = data.decode(charset, errors="replace")
                except LookupError:
                    text = data.decode("utf-8", errors="replace")
                return text, url
        raise ValueError("网站重定向次数过多")

    async def fetch(self, url):
        parts = urlsplit(validate_source_url(url))
        origin = f"{parts.scheme}://{parts.netloc}"
        async with self.locks.setdefault(origin, asyncio.Lock()):
            if origin not in self.robots:
                robot = RobotFileParser(origin + "/robots.txt")
                try:
                    text, _ = await self.request(origin + "/robots.txt")
                    robot.parse(text.splitlines())
                except ValueError as error:
                    if "HTTP 404" in str(error):
                        robot.parse([])
                    else:
                        raise ValueError("无法校验网站 robots.txt") from error
                self.robots[origin] = robot
            if not self.robots[origin].can_fetch(AGENT, url):
                raise ValueError("网站 robots.txt 禁止抓取此页面")
            await asyncio.sleep(max(0.7, self.robots[origin].crawl_delay(AGENT) or 0))
            for attempt in range(2):
                try:
                    return await self.request(url)
                except (aiohttp.ClientError, TimeoutError):
                    if attempt:
                        raise
                    await asyncio.sleep(1)

    async def discover(self, task):
        candidates = []
        errors = []
        start, end = [parse_date(v).astimezone(CHINA_TIME) for v in task["timeRange"]]
        topics = [k for k in task["keywords"] if k != task["regionKeyword"]][:3]
        for topic in topics:
            query = f'{task["regionKeyword"]} {topic} after:{start:%Y-%m-%d} before:{end + timedelta(days=1):%Y-%m-%d}'
            for provider in ("bing", "baidu"):
                if provider == "bing":
                    url = "https://www.bing.com/search?" + urlencode({"q": query, "count": 50})
                else:
                    url = "https://www.baidu.com/s?" + urlencode({"tn": "news", "word": f'{task["regionKeyword"]} {topic}',
                        "rtt": 4, "bt": int(start.timestamp()), "et": int(end.timestamp())})
                try:
                    html, final_url = await self.fetch(url)
                    candidates.extend(search_links(html, final_url))
                except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                    reason = str(error)[:160] if isinstance(error, ValueError) else "网络连接失败或超时"
                    errors.append(f"{provider} / {topic}：{reason}")
        return list({c["url"]: c for c in candidates}.values()), errors


def seed_candidates(task):
    # Seeds are URLs, never bundled article content or generated records.
    urls = list(task.get("seedUrls", []))
    if task["regionKeyword"] == "郑州" and any(word in normalize(task["eventName"]) for word in ("暴雨", "洪涝", "洪水")):
        urls.extend(json.loads((Path(__file__).parent / "zhengzhou_seeds.json").read_text()))
    return [{"url": url} for url in dict.fromkeys(urls)]
