"""Validated collection configuration; browser dates are Asia/Shanghai wall time."""
from datetime import datetime, timezone, timedelta
import re

CHINA_TIME = timezone(timedelta(hours=8))


def normalize(value):
    return re.sub(r"[^\w]", "", value.lower())


def parse_date(value):
    if not isinstance(value, str):
        raise ValueError("时间必须是 ISO 日期字符串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=CHINA_TIME)).astimezone(timezone.utc)
    except ValueError:
        raise ValueError("时间格式无效") from None


def validate_task(payload):
    if not isinstance(payload, dict):
        raise ValueError("请求体必须是 JSON 对象")
    result = {}
    for key in ("eventName", "regionId", "location", "regionKeyword"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ValueError(f"{key} 必须是 1—200 字符的文本")
        result[key] = value.strip()
    keywords = payload.get("keywords")
    if not isinstance(keywords, list) or not 1 <= len(keywords) <= 20 or any(
        not isinstance(v, str) or not 2 <= len(v.strip()) <= 80 for v in keywords
    ):
        raise ValueError("请提供 1—20 个关键词，每个 2—80 字符")
    result["keywords"] = list(dict.fromkeys(v.strip() for v in keywords))
    if not any(normalize(v) != normalize(result["regionKeyword"]) for v in result["keywords"]):
        raise ValueError("请提供至少一个地区以外的事件主题关键词")
    dates = payload.get("timeRange")
    if not isinstance(dates, list) or len(dates) != 2:
        raise ValueError("请选择完整的采集时间窗")
    start, end = map(parse_date, dates)
    if end <= start:
        raise ValueError("结束时间必须晚于开始时间")
    result.update(timeRange=[start.isoformat(), end.isoformat()], timeWindow=" 至 ".join(dates))
    limit = payload.get("maxRecords", 30)
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("目标条数必须是 1—500 的整数")
    result["maxRecords"] = limit
    urls = payload.get("seedUrls", [])
    if not isinstance(urls, list) or len(urls) > 50:
        raise ValueError("补充来源链接最多 50 条")
    from ..connectors.web_sources import validate_source_url
    result["seedUrls"] = list(dict.fromkeys(validate_source_url(url) for url in urls))
    source = payload.get("configSource", "人工配置")
    if not isinstance(source, str) or len(source) > 200:
        raise ValueError("配置来源无效")
    result["configSource"] = source
    return result
