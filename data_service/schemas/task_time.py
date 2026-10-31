"""Canonicalize explicit date ranges without guessing missing dates or years."""
import re
from datetime import datetime


def normalize_explicit_time_window(value):
    if not isinstance(value, str):
        return value
    date = r'(\d{4})\s*[-./年]\s*(\d{1,2})\s*[-./月]\s*(\d{1,2})\s*日?\s*[T ]\s*(\d{1,2})[:：](\d{2})'
    match = re.fullmatch(date + r'\s*(?:至|到|~|～|—|–|-|→)\s*' + date, value.strip())
    if not match:
        return value
    numbers = list(map(int, match.groups()))
    try:
        start, end = datetime(*numbers[:5]), datetime(*numbers[5:])
    except ValueError:
        raise ValueError('时间窗包含无效日期或时刻，请检查年月日与小时分钟') from None
    if end <= start:
        raise ValueError('任务结束时间须晚于开始时间')
    return start.strftime('%Y-%m-%d %H:%M') + ' 至 ' + end.strftime('%Y-%m-%d %H:%M')


def explicit_time_request(content):
    """Recognize a complete, single-purpose time edit without guessing years."""
    date = r'(\d{4})\s*(?:年|-)\s*(\d{1,2})\s*(?:月|-)\s*(\d{1,2})\s*日?\s*(凌晨|早上|上午|中午|下午|晚上|晚)?\s*(\d{1,2})(?:[:：](\d{2})|点(?:(\d{1,2})分?)?)'
    prefix = r'(?:(?:我想|我要|请|帮我)?\s*(?:把)?(?:研究|分析)?时间(?:窗)?(?:选择|设置|修改|调整|改)?(?:为|成|是)?\s*)?'
    match = re.fullmatch(prefix + date + r'\s*(?:至|到)\s*' + date + r'\s*(?:吧|。|！|!|\.)*', content.strip())
    if not match:
        return None
    def endpoint(parts):
        year, month, day, period, hour, minute, chinese_minute = parts
        hour = int(hour)
        if period in ('下午', '晚上', '晚'):
            if not 1 <= hour <= 12:
                raise ValueError('下午或晚间时刻须使用1至12点')
            if hour < 12:
                hour += 12
        elif period in ('凌晨', '早上', '上午') and hour == 12:
            hour = 0
        elif period == '中午' and hour < 11:
            raise ValueError('中午时刻含义不明确，请使用24小时制时间')
        return datetime(int(year), int(month), int(day), hour, int(minute or chinese_minute or 0))
    try:
        start, end = endpoint(match.groups()[:7]), endpoint(match.groups()[7:])
    except ValueError:
        raise ValueError('请检查完整起止日期和时刻，或使用时间选择器') from None
    if end <= start:
        raise ValueError('任务结束时间须晚于开始时间')
    return f'{start:%Y-%m-%d %H:%M} 至 {end:%Y-%m-%d %H:%M}'


def time_window_duration(value):
    start, end = (datetime.strptime(part, '%Y-%m-%d %H:%M') for part in value.split(' 至 '))
    minutes = int((end - start).total_seconds() / 60)
    hours, remainder = divmod(minutes, 60)
    return (f'{hours}小时' if hours else '') + (f'{remainder}分钟' if remainder else '')
