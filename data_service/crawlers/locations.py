"""Match original-text mentions to a reviewed, region-scoped geographic catalog."""
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def place_catalog():
    return json.loads(Path(__file__).with_name("news_places.json").read_text(encoding="utf-8"))


def article_locations(title, content, region_id):
    catalog = place_catalog()
    text = title + "\n" + content
    points = []
    for place in catalog["regions"].get(region_id, []):
        matches = [(text.find(alias), alias) for alias in place["aliases"] if alias in text]
        if not matches:
            continue
        position, alias = min(matches)
        points.append((position, {**{key: value for key, value in place.items() if key != "aliases"},
                                  "matchedText": alias,
                                  "evidence": text[max(0, position - 35):position + len(alias) + 75].strip()}))
    # A report may mention several places. Never turn a line name or city name into a POI.
    return {"locationPoints": [point for _, point in sorted(points, key=lambda match: match[0])],
            "locationPrecision": "mentioned_place" if points else "task_region",
            "locationVersion": catalog["version"]}
