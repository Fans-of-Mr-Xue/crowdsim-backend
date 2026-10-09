"""Validation for shared dataset documents."""
import math


def validate_dataset(payload):
    if not isinstance(payload, dict):
        raise ValueError("数据集必须是 JSON 对象")
    unknown = set(payload) - {"name", "source", "description", "records"}
    if unknown:
        raise ValueError("不支持的字段：" + ", ".join(sorted(unknown)))
    result = {}
    for key, limit in (("name", 200), ("source", 200), ("description", 4000)):
        value = payload.get(key, "")
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"{key} 必须是长度不超过 {limit} 的文本")
        result[key] = value.strip()
    if not result["name"]:
        raise ValueError("请输入数据集名称")
    records = payload.get("records", [])
    if not isinstance(records, list) or len(records) > 10000 or any(not isinstance(row, dict) for row in records):
        raise ValueError("records 必须是最多 10000 条 JSON 对象组成的数组")
    def check_keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or key.startswith("$") or "." in key or "\x00" in key:
                    raise ValueError("数据字段不能包含点、空字符或以 $ 开头")
                check_keys(child)
        elif isinstance(value, list):
            for child in value:
                check_keys(child)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("数值必须是有限数")
        elif type(value) is int and not -(2 ** 63) <= value < 2 ** 63:
            raise ValueError("整数必须在 64 位有符号范围内")
    check_keys(records)
    result["records"] = records
    result["record_count"] = len(records)
    return result


