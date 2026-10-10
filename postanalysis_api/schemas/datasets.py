"""Dataset import contract: normalized timeline, never an executable file path."""

from __future__ import annotations

from .common import array, choice, integer, object_, optional_number, text_, fail


def validate_dataset(payload: dict) -> dict:
    data = object_(payload, "body", keys={"name", "sourceKind", "sourceNote", "observations"})
    name = text_(data.get("name"), "name", max_len=120)
    source = choice(data.get("sourceKind"), "sourceKind", {"synthetic_reference", "observed"})
    note = text_(data.get("sourceNote"), "sourceNote", min_len=1, max_len=500)
    observations = array(data.get("observations"), "observations", 1, 50000)
    rows = []
    seen = set()
    for index, raw in enumerate(observations):
        field = f"observations[{index}]"
        row = object_(raw, field, keys={"timeSeconds", "regionId", "population", "density", "meanSpeed", "pressureProxy", "sourceTime", "state"})
        time = integer(row.get("timeSeconds"), f"{field}.timeSeconds", 0, 86400)
        region = text_(row.get("regionId"), f"{field}.regionId", max_len=64,
                       pattern=r"[A-Za-z0-9][A-Za-z0-9_-]*")
        key = (time, region)
        if key in seen:
            fail(field, "相同区域和时间只能有一条记录", "DUPLICATE_OBSERVATION")
        seen.add(key)
        rows.append({"timeSeconds": time, "regionId": region,
                     "population": integer(row.get("population"), f"{field}.population", 0, 1000000),
                     "density": optional_number(row.get("density"), f"{field}.density", 0, 1000),
                     "meanSpeed": optional_number(row.get("meanSpeed"), f"{field}.meanSpeed", 0, 30),
                     "pressureProxy": optional_number(row.get("pressureProxy"), f"{field}.pressureProxy", 0, 1e9),
                     "sourceTime": text_(row.get("sourceTime", str(time)), f"{field}.sourceTime", max_len=80),
                     "state": text_(row.get("state", ""), f"{field}.state", min_len=0, max_len=500)})
    rows.sort(key=lambda item: (item["timeSeconds"], item["regionId"]))
    return {"name": name, "sourceKind": source, "sourceNote": note, "observations": rows}
