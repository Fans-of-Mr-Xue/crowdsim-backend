"""Read completed during archives without changing the during service or data."""
import csv
import gzip
import hashlib
import io
import math
import re

from bson import ObjectId
from bson.errors import InvalidId
from gridfs import GridFS, NoFile
from postanalysis_api.schemas.common import ApiError


class DuringArchiveReader:
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
        value = cls.descriptor(document) or {}
        return hashlib.sha256(f"{document['run_id']}:{document.get('updated_at')}:{value.get('sha256')}:{value.get('file_id')}".encode()).hexdigest()

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
            with self.files.get(ObjectId(descriptor["file_id"])) as source:
                metadata = source.metadata or {}
                if metadata.get("run_id") != run_id or metadata.get("role") != "observation_global":
                    raise ValueError("观测文件与实验绑定不一致")
                compressed = source.read(8 * 1024 * 1024 + 1)
            digest = hashlib.sha256(compressed).hexdigest()
            if len(compressed) > 8 * 1024 * 1024 or digest != descriptor.get("sha256") or digest != metadata.get("sha256"):
                raise ValueError("观测归档大小或校验值异常")
            if descriptor.get("compression") != "gzip":
                raise ValueError("观测归档压缩格式不支持")
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
                content = stream.read(16 * 1024 * 1024 + 1)
            if len(content) > 16 * 1024 * 1024:
                raise ValueError("全局观测文件超过读取上限")
            reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""))
            required = {"time_seconds", "snapshot_id", "person_count", "density_person_per_m2", "avg_speed_mps"}
            if not required.issubset(reader.fieldnames or []):
                raise ValueError("全局观测列不完整")
            series = []
            for row in reader:
                if any(row.get(key) is None for key in required):
                    raise ValueError("观测行存在缺失列")
                time, population = float(row["time_seconds"]), float(row["person_count"])
                def metric(key):
                    value = float(row[key]) if row[key].strip() else None
                    if value is not None and (not math.isfinite(value) or value < 0):
                        raise ValueError("观测指标存在非有限数值或负值")
                    return value
                if (not math.isfinite(time) or not 0 <= time <= 86400 or not math.isfinite(population)
                        or not population.is_integer() or not 0 <= population <= 1000000
                        or series and time <= series[-1]["timeSeconds"] or len(series) >= 50000
                        or not row["snapshot_id"].startswith(run_id + ":")):
                    raise ValueError("观测时间、人数或快照绑定无效")
                series.append({"timeSeconds": time, "personCount": int(population), "sourceSnapshotId": row["snapshot_id"],
                               "global-density": metric("density_person_per_m2"), "global-speed": metric("avg_speed_mps"), "pressureProxy": None})
            if not series or len(series) != document.get("summary", {}).get("recorded_samples"):
                raise ValueError("观测采样数与归档摘要不一致")
        except (ValueError, KeyError, OSError, EOFError, UnicodeError, InvalidId, csv.Error, NoFile) as exc:
            raise ApiError("DURING_ARCHIVE_INVALID", f"事中原始观测校验失败：{exc}", 422) from None
        return {"sourceKind": "mongodb_archive", "sourceVersion": self.version(document), "executionVersion": 0,
                "reportVersion": self.version(document), "observationIntervalSeconds": document.get("summary", {}).get("sample_interval_seconds"),
                "sourceBinding": {"sourcePhase": "during", "sourceKind": "mongodb_archive", "collection": "during_results",
                                  "runId": run_id, "fileId": str(descriptor["file_id"]), "sha256": digest, "sourceVersion": self.version(document)},
                "limitations": ["只导入归档的全局观测；局部网格与轨迹仍保留在原始归档。", "未记录压力代理，保留为空；不推测未归档的种子、位置或时钟起点。"],
                "runs": [{"runId": run_id, "status": "completed", "valid": True, "seed": document.get("seed"), "series": series}]}
