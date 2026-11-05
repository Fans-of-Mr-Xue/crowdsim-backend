"""Read completed during archives without changing the during service or data."""
import csv
import gzip
import hashlib
import io
import json
import math
import re

from bson import ObjectId
from bson.errors import InvalidId
from gridfs import GridFS, NoFile
from postanalysis_api.schemas.common import ApiError


class DuringArchiveReader:
    ROLES = {"observation_global", "observation_cells", "observation_boundaries", "observation_samples"}
    def __init__(self, database, files=None):
        self.db = database
        self.files = files if files is not None else GridFS(database, collection="during_result_files")

    @staticmethod
    def visible(document, user):
        return all(str(document[key]) == user for key in ("ownerId", "owner_id", "user_id") if document.get(key) is not None)

    @staticmethod
    def ready(document):
        return document.get("run_status") == "completed" and document.get("archive_status") == "ready" and document.get("data_complete") is True

    @staticmethod
    def descriptor(document):
        return next((item for item in document.get("files", []) if item.get("role") == "observation_global" and item.get("original_name") == "observation_global.csv"), None)

    @classmethod
    def version(cls, document):
        files = sorted((item.get("role"), str(item.get("file_id")), item.get("sha256"))
                       for item in document.get("files", []) if item.get("role") in cls.ROLES)
        return hashlib.sha256(f"{document['run_id']}:{document.get('updated_at')}:{files}".encode()).hexdigest()

    def _file(self, document, role, provenance):
        matches = [item for item in document.get("files", []) if item.get("role") == role]
        if len(matches) != 1:
            raise ValueError(f"{role}文件缺失或重复")
        descriptor = matches[0]
        extension = "jsonl" if role == "observation_samples" else "csv"
        if descriptor.get("original_name") != f"{role}.{extension}" or descriptor.get("compression") != "gzip":
            raise ValueError(f"{role}文件名称或压缩格式不支持")
        with self.files.get(ObjectId(descriptor["file_id"])) as source:
            metadata = source.metadata or {}
            if metadata.get("run_id") != document["run_id"] or metadata.get("role") != role:
                raise ValueError("观测文件与实验绑定不一致")
            compressed = source.read(8 * 1024 * 1024 + 1)
        digest = hashlib.sha256(compressed).hexdigest()
        if len(compressed) > 8 * 1024 * 1024 or digest != descriptor.get("sha256") or digest != metadata.get("sha256"):
            raise ValueError("观测归档大小或校验值异常")
        provenance.append({"role": role, "fileId": str(descriptor["file_id"]), "sha256": digest})
        return compressed

    @staticmethod
    def _lines(compressed):
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            size = 0
            while line := stream.readline(1024 * 1024 + 1):
                size += len(line)
                if len(line) > 1024 * 1024 or size > 128 * 1024 * 1024:
                    raise ValueError("观测文件超过读取上限")
                yield line.decode("utf-8-sig" if size == len(line) else "utf-8")

    def _csv(self, document, role, fields, provenance):
        reader = csv.DictReader(self._lines(self._file(document, role, provenance)))
        if not fields.issubset(reader.fieldnames or []):
            raise ValueError(f"{role}观测列不完整")
        for index, row in enumerate(reader):
            if index >= 1000000 or any(row.get(key) is None for key in fields):
                raise ValueError("观测行数超限或存在缺失列")
            yield row

    @staticmethod
    def _metric(row, key):
        value = float(row[key]) if row[key].strip() else None
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("观测指标存在非有限数值或负值")
        return value

    def _spatial(self, document, series, provenance):
        roles = {item.get("role") for item in document.get("files", [])}
        selected = roles & {"observation_cells", "observation_boundaries"}
        if not selected:
            return ["归档未包含局部网格与边界文件，相关指标保留为空。"]
        points = {row["sourceSnapshotId"]: row for row in series}
        counts = {key: {"cell_rows": 0, "boundary_rows": 0} for key in points}
        for role, id_key, count_key, fields in (
            ("observation_cells", "cell_id", "cell_rows", {"density_person_per_m2", "avg_speed_mps"}),
            ("observation_boundaries", "boundary_id", "boundary_rows", {"absolute_difference_person_per_m2", "status"}),
        ):
            if role not in selected:
                continue
            seen = set()
            for row in self._csv(document, role, fields | {"time_seconds", "snapshot_id", id_key}, provenance):
                snapshot = row["snapshot_id"]
                point = points.get(snapshot)
                key = (snapshot, row[id_key])
                if not point or float(row["time_seconds"]) != point["timeSeconds"] or not row[id_key] or key in seen:
                    raise ValueError("局部观测时间、快照或标识无效/重复")
                seen.add(key)
                counts[snapshot][count_key] += 1
                if role == "observation_cells":
                    for source, target, operation in (("density_person_per_m2", "local-density", max), ("avg_speed_mps", "local-speed", min)):
                        value = self._metric(row, source)
                        if value is not None:
                            point[target] = value if point[target] is None else operation(point[target], value)
                elif row["status"] == "valid":
                    value = self._metric(row, "absolute_difference_person_per_m2")
                    if value is None:
                        raise ValueError("有效边界缺少密度差")
                    point["boundary-density-difference"] = value if point["boundary-density-difference"] is None else max(point["boundary-density-difference"], value)
        committed = set()
        for line in self._lines(self._file(document, "observation_samples", provenance)):
            marker = json.loads(line)
            snapshot = marker["snapshot_id"]
            if snapshot not in points or snapshot in committed or marker["time_seconds"] != points[snapshot]["timeSeconds"]:
                raise ValueError("观测提交记录与采样快照不一致")
            for role, field in (("observation_cells", "cell_rows"), ("observation_boundaries", "boundary_rows")):
                if role in selected and (type(marker.get(field)) is not int or counts[snapshot][field] != marker[field]):
                    raise ValueError("局部观测行数与提交记录不一致")
            committed.add(snapshot)
        if committed != set(points):
            raise ValueError("观测提交记录不完整")
        return [] if len(selected) == 2 else ["部分局部/边界观测文件未归档，对应指标保留为空。"]

    def catalog(self, user):
        query = {"run_status": "completed", "archive_status": "ready", "data_complete": True}
        return [{"sourceKind": "mongodb_archive", "runId": row["run_id"], "key": "archive:" + row["run_id"],
                 "sourceVersion": self.version(row), "completedRunCount": 1,
                 "label": f"数据库事中归档 · {row['run_id']} · {row.get('simulated_seconds', '?')}秒"}
                for row in self.db.during_results.find(query).sort("updated_at", -1).limit(200)
                if self.visible(row, user) and self.descriptor(row)]

    def report(self, run_id, user):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", run_id):
            raise ApiError("NOT_FOUND", "事中归档不存在", 404)
        document = self.db.during_results.find_one({"run_id": run_id})
        if not document or not self.visible(document, user):
            raise ApiError("NOT_FOUND", "事中归档不存在或不可读取", 404)
        if not self.ready(document):
            raise ApiError("DURING_ARCHIVE_NOT_READY", "只可读取已完成且归档完整的事中实验", 422)
        descriptor = self.descriptor(document)
        if not descriptor:
            raise ApiError("DURING_ARCHIVE_INVALID", "事中归档缺少全局原始观测", 422)
        try:
            provenance = []
            required = {"time_seconds", "snapshot_id", "person_count", "density_person_per_m2", "avg_speed_mps"}
            series = []
            snapshots = set()
            for row in self._csv(document, "observation_global", required, provenance):
                time, population = float(row["time_seconds"]), float(row["person_count"])
                if (not math.isfinite(time) or not 0 <= time <= 86400 or not math.isfinite(population)
                        or not population.is_integer() or not 0 <= population <= 1000000
                        or series and time <= series[-1]["timeSeconds"] or len(series) >= 50000
                        or not row["snapshot_id"].startswith(run_id + ":") or row["snapshot_id"] in snapshots):
                    raise ValueError("观测时间、人数或快照绑定无效")
                snapshots.add(row["snapshot_id"])
                series.append({"timeSeconds": time, "personCount": int(population), "sourceSnapshotId": row["snapshot_id"],
                               "global-density": self._metric(row, "density_person_per_m2"), "global-speed": self._metric(row, "avg_speed_mps"), "pressureProxy": None,
                               "local-density": None, "local-speed": None, "boundary-density-difference": None})
            if not series or len(series) != document.get("summary", {}).get("recorded_samples"):
                raise ValueError("观测采样数与归档摘要不一致")
            limitations = self._spatial(document, series, provenance)
        except (ValueError, TypeError, KeyError, OSError, EOFError, UnicodeError, InvalidId, csv.Error, NoFile) as exc:
            raise ApiError("DURING_ARCHIVE_INVALID", f"事中原始观测校验失败：{exc}", 422) from None
        return {"sourceKind": "mongodb_archive", "sourceVersion": self.version(document), "executionVersion": 0,
                "reportVersion": self.version(document), "observationIntervalSeconds": document.get("summary", {}).get("sample_interval_seconds"),
                "sourceBinding": {"sourcePhase": "during", "sourceKind": "mongodb_archive", "collection": "during_results",
                                  "runId": run_id, "fileId": str(descriptor["file_id"]), "sha256": descriptor["sha256"], "sourceVersion": self.version(document), "files": provenance},
                "metricSemantics": {"local-density": "同一采样时刻有效网格密度最大值（人/m²）", "local-speed": "同一采样时刻有效网格平均速度最小值（m/s），空网格不补零",
                                    "boundary-density-difference": "同一采样时刻status=valid的相邻网格边界密度绝对差最大值（人/m²）"},
                "limitations": limitations + ["保留全局与局部摘要；原网格、边界明细及轨迹仍留在原始归档。", "未记录压力代理，保留为空；不推测未归档的种子、位置或时钟起点。"],
                "runs": [{"runId": run_id, "status": "completed", "valid": True, "seed": document.get("seed"), "series": series}]}
