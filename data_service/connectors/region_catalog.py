"""Local province/city names for selectors; never used as simulation geometry."""
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def city_catalog():
    return json.loads((Path(__file__).resolve().parents[1] / 'resources/china_cities.json').read_text(encoding='utf-8'))


def find_city(code):
    for province in city_catalog()['provinces']:
        for city in province['cities']:
            if city['code'] == str(code):
                return {**city, 'province': province['name'], 'province_code': province['code']}
    raise ValueError('请先选择有效的省、市')
