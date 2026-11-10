"""Population distributions are reconstruction targets, not historical replay inputs."""
from collections import Counter


def distribution_targets(dataset):
    rows = dataset.get("observations") or []
    spatial = [row for row in rows if row.get("regionId") not in (None, "", "global")]
    regions = sorted({row["regionId"] for row in spatial})
    times = sorted({row["timeSeconds"] for row in spatial})
    coverage = Counter(row["regionId"] for row in spatial)
    missing = []
    context = dataset.get('metadata', {}).get('factualContext')
    if context:
        expected_regions = set(context['expectedRegionIds'])
        expected_times = set(context['expectedTimes'])
        keys = {(row['timeSeconds'], row['regionId']) for row in spatial}
        if (set(regions) != expected_regions or set(times) != expected_times or len(keys) != len(spatial) or
                len(keys) != len(expected_regions) * len(expected_times)):
            missing.append('原始期望网格/时间覆盖不完整或存在未知、重复标识，不得丢格、丢时刻或补零')
        counts = Counter(row['regionId'] for row in context['initialDistribution'])
        if times and any(counts[row['regionId']] != row['population'] for row in spatial if row['timeSeconds'] == times[0]):
            missing.append('初始人群分布与首帧空间目标不一致')
    if not spatial:
        missing.append("按step保存的路口/空间单元人数分布；只有全局总人数不能确定空间分布")
    elif len(times) < 2:
        missing.append("至少两个时刻的人数分布，用于核对演化过程")
    if times and any(count != len(times) for count in coverage.values()):
        missing.append("各时刻完整的空间单元人数；真实零人数应记0，缺记录不能自动当作0")
    return {"mode": "reconstruct_from_distribution", "inputReady": not missing,
            "requiredObservationFields": ["timeSeconds", "regionId", "population"],
            "regionIds": regions, "stepCount": len(times), "observationCount": len(spatial),
            "timeWindow": {"startSeconds": times[0], "endSeconds": times[-1]} if times else None,
            "missing": missing,
            "spatialMappingSource": "current_post_environment",
            "actionCapabilitiesSource": "/api/v1/post/capabilities",
            "note": "用当前事后人工社会生成并校准F-00，按空间单元与时刻匹配人数分布。"}
