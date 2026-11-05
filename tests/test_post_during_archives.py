from copy import deepcopy
import gzip
import hashlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from bson import ObjectId
import json

from postanalysis_api.services.during_archives import DuringArchiveReader
from postanalysis_api.schemas.common import ApiError
from postanalysis_api.api.common import Request
from postanalysis_api.api.datasets import handle


class Cursor(list):
    def sort(self, key, direction):
        return Cursor(sorted(self, key=lambda row: str(row.get(key, "")), reverse=direction < 0))

    def limit(self, size):
        return self[:size]


class Collection:
    def __init__(self, documents):
        self.documents = documents

    def find(self, query):
        return Cursor(deepcopy([row for row in self.documents if all(row.get(key) == value for key, value in query.items())]))

    def find_one(self, query):
        return next(iter(self.find(query)), None)


class DuringArchiveTests(unittest.TestCase):
    def setUp(self):
        self.csv = 'time_seconds,snapshot_id,person_count,density_person_per_m2,avg_speed_mps\n0.5,run-one:0,10,2,0\n1.0,run-one:1,0,0,\n'
        self.content = gzip.compress(self.csv.encode())
        self.digest = hashlib.sha256(self.content).hexdigest()
        self.document = {"run_id": "run-one", "run_status": "completed", "archive_status": "ready", "data_complete": True,
            "simulated_seconds": 1, "updated_at": "2026-11-05", "summary": {"recorded_samples": 2, "sample_interval_seconds": .5},
            "files": [{"role": "observation_global", "original_name": "observation_global.csv", "compression": "gzip",
                       "file_id": "6aeb3b8c2f2905828e1d1b2c", "sha256": self.digest}]}
        self.metadata = {"run_id": "run-one", "role": "observation_global", "sha256": self.digest}
        self.documents = [self.document]
        self.db = SimpleNamespace(during_results=Collection(self.documents))
        self.reader = DuringArchiveReader(self.db, SimpleNamespace(get=self.file))

    def file(self, _id):
        result = io.BytesIO(self.content)
        result.metadata = self.metadata
        return result

    def test_completed_archive_reads_exact_samples_without_mutating_source(self):
        before = deepcopy(self.document)
        report = self.reader.report("run-one", "user")
        points = report["runs"][0]["series"]
        self.assertEqual([.5, 1.0], [row["timeSeconds"] for row in points])
        self.assertEqual(0, points[0]["global-speed"])
        self.assertEqual(0, points[-1]["personCount"])
        self.assertIsNone(points[-1]["global-speed"])
        self.assertTrue(all(row["pressureProxy"] is None for row in points))
        self.assertIsNone(report["runs"][0]["seed"])
        self.assertEqual(self.digest, report["sourceBinding"]["sha256"])
        self.assertEqual(before, self.document)

    def test_real_bson_file_identifiers_are_safe_in_the_http_response(self):
        self.document["files"][0]["file_id"] = ObjectId(self.document["files"][0]["file_id"])
        report = self.reader.report("run-one", "user")
        self.assertIsInstance(report["sourceBinding"]["fileId"], str)
        json.dumps(report, allow_nan=False)

    def test_catalog_filters_incomplete_failed_and_foreign_owned_archives(self):
        for patch_value in ({"run_id": "failed", "run_status": "failed"}, {"run_id": "partial", "data_complete": False},
                            {"run_id": "foreign", "ownerId": "other"}, {"run_id": "own", "ownerId": "user"}):
            self.documents.append({**deepcopy(self.document), **patch_value})
        self.assertEqual({"run-one", "own"}, {row["runId"] for row in self.reader.catalog("user")})
        with self.assertRaises(ApiError) as denied:
            self.reader.report("foreign", "user")
        self.assertEqual(404, denied.exception.status)
        with self.assertRaises(ApiError):
            self.reader.report("partial", "user")

    def test_corrupt_hash_binding_and_sample_count_are_not_silently_accepted(self):
        for change in (lambda: self.metadata.update(run_id="other"), lambda: self.document["files"][0].update(sha256="wrong"),
                       lambda: self.document["summary"].update(recorded_samples=3)):
            self.setUp(); change()
            with self.assertRaises(ApiError) as failure:
                self.reader.report("run-one", "user")
            self.assertEqual("DURING_ARCHIVE_INVALID", failure.exception.code)

    def test_duplicate_time_or_wrong_snapshot_binding_is_rejected(self):
        for content in (self.csv.replace('1.0,run-one:1', '0.5,run-one:1'), self.csv.replace('run-one:1', 'other:1')):
            self.content = gzip.compress(content.encode())
            digest = hashlib.sha256(self.content).hexdigest()
            self.metadata["sha256"] = self.document["files"][0]["sha256"] = digest
            with self.assertRaises(ApiError):
                self.reader.report("run-one", "user")

    def test_post_routes_read_catalog_and_report_with_the_current_user(self):
        app = SimpleNamespace(repo=SimpleNamespace(db=self.db))
        headers = {"X-CrowdSim-User": "user", "X-CrowdSim-Workspace": "post-target"}
        with patch('postanalysis_api.services.during_archives.DuringArchiveReader', return_value=self.reader):
            status, catalog, _ = handle(Request('GET', '/during-sources', {}, {}, headers, app))
            self.assertEqual(200, status)
            self.assertEqual("run-one", catalog[0]["runId"])
            _, report, _ = handle(Request('GET', '/during-sources/run-one', {}, {}, headers, app))
            self.assertEqual(2, len(report["runs"][0]["series"]))
