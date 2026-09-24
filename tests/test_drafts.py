from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from robot_trials.clock import FrozenClock
from robot_trials.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from robot_trials.jsonio import load_json
from robot_trials.service import TrialService, protocol_content
from robot_trials.storage import SCHEMA_SQL, connect, initialize, inspect_schema


ROOT = Path(__file__).resolve().parents[1]


class DraftLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = TrialService(self.connection, self.clock)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("approver", "approver"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.protocol = load_json(ROOT / "fixtures" / "demo_protocol.json")
        self.content = protocol_content(self.protocol)
        self.service.register_robot("operator", "robot-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "robot-a", "1.0", "b" * 64)

    def tearDown(self) -> None:
        self.connection.close()

    def _publish_v1(self) -> dict:
        draft = self.service.create_draft("stat", "draft-1", self.content)
        self.assertEqual(draft["draft_revision"], 1)
        self.assertIsNone(draft["base_version"])
        return self.service.publish_draft("stat", "draft-1", 1)

    def test_create_and_publish_first_version(self) -> None:
        published = self._publish_v1()
        self.assertEqual(published["version"], 1)
        draft = self.service.get_draft("draft-1")
        self.assertEqual(draft["status"], "published")
        self.assertEqual(draft["published_version"], 1)
        version = self.service.get_protocol(self.protocol["protocol_id"], 1)
        self.assertEqual(version["status"], "active")

    def test_only_statistician_manages_drafts(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_draft("operator", "draft-x", self.content)
        self._publish_v1()
        with self.assertRaises(Forbidden):
            self.service.derive_draft("operator", "draft-x", self.protocol["protocol_id"], 1)
        with self.assertRaises(Forbidden):
            self.service.retire_protocol("operator", self.protocol["protocol_id"], 1, "理由")

    def test_derive_from_published_and_publish_next_version(self) -> None:
        self._publish_v1()
        derived = self.service.derive_draft("stat", "draft-2", self.protocol["protocol_id"], 1)
        self.assertEqual(derived["base_version"], 1)
        revised_content = copy.deepcopy(self.content)
        revised_content["title"] = "室内递送基础重复试验（修订）"
        revised = self.service.revise_draft("stat", "draft-2", 1, revised_content)
        self.assertEqual(revised["draft_revision"], 2)
        published = self.service.publish_draft("stat", "draft-2", 2)
        self.assertEqual(published["version"], 2)

    def test_concurrent_revision_is_rejected(self) -> None:
        self._publish_v1()
        self.service.derive_draft("stat", "draft-2", self.protocol["protocol_id"], 1)
        first_content = copy.deepcopy(self.content)
        first_content["title"] = "标题修改 A"
        self.service.revise_draft("stat", "draft-2", 1, first_content)
        stale_content = copy.deepcopy(self.content)
        stale_content["title"] = "标题修改 B"
        with self.assertRaises(Conflict):
            self.service.revise_draft("stat", "draft-2", 1, stale_content)
        with self.assertRaises(Conflict):
            self.service.publish_draft("stat", "draft-2", 1)
        current = self.service.get_draft("draft-2")
        self.assertEqual(current["content"]["title"], "标题修改 A")
        self.assertEqual(current["draft_revision"], 2)

    def test_publish_rejects_non_contiguous_version(self) -> None:
        # 直接发布 v2（没有 v1）必须被连续性校验拒绝。
        gap = copy.deepcopy(self.protocol)
        gap["version"] = 2
        with self.assertRaises(Conflict):
            self.service.publish_protocol("stat", gap)

    def test_publish_rejects_publishing_draft_built_on_stale_base(self) -> None:
        self._publish_v1()
        # draft-2 派生自 v1。
        self.service.derive_draft("stat", "draft-2", self.protocol["protocol_id"], 1)
        # 另一条线先发布了 v2，使 draft-2 的基线过期。
        other = self.service.derive_draft("stat", "draft-3", self.protocol["protocol_id"], 1)
        changed = copy.deepcopy(self.content)
        changed["bootstrap_samples"] = 600
        other_revised = self.service.revise_draft("stat", "draft-3", other["draft_revision"], changed)
        self.service.publish_draft("stat", "draft-3", other_revised["draft_revision"])
        changed_two = copy.deepcopy(self.content)
        changed_two["bootstrap_samples"] = 700
        self.service.revise_draft("stat", "draft-2", 1, changed_two)
        with self.assertRaisesRegex(Conflict, "不是最新版本"):
            self.service.publish_draft("stat", "draft-2", 2)

    def test_content_digest_uniqueness_blocks_duplicate_publish(self) -> None:
        self._publish_v1()
        # 派生但不修改任何内容，发布时必须因摘要重复被拒绝。
        derived = self.service.derive_draft("stat", "draft-2", self.protocol["protocol_id"], 1)
        with self.assertRaises(Conflict):
            self.service.publish_draft("stat", "draft-2", derived["draft_revision"])

    def test_create_draft_rejects_identical_published_content(self) -> None:
        self._publish_v1()
        with self.assertRaises(Conflict):
            self.service.create_draft("stat", "draft-dup", self.content)

    def test_rule_completeness_validated_on_revise_and_publish(self) -> None:
        draft = self.service.create_draft("stat", "draft-1", self.content)
        broken = copy.deepcopy(self.content)
        broken["admission_rules"] = []
        with self.assertRaises(ValidationFailed):
            self.service.revise_draft("stat", "draft-1", draft["draft_revision"], broken)
        unknown_rule = copy.deepcopy(self.content)
        unknown_rule["admission_rules"][0] = dict(unknown_rule["admission_rules"][0])
        unknown_rule["admission_rules"][0]["metric"] = "not-declared"
        with self.assertRaises(ValidationFailed):
            self.service.revise_draft("stat", "draft-1", 1, unknown_rule)
        bad_weights = copy.deepcopy(self.content)
        bad_weights["stratum_weights"] = {"clear-aisle": "0.4", "cross-traffic": "0.5"}
        with self.assertRaises(ValidationFailed):
            self.service.revise_draft("stat", "draft-1", 1, bad_weights)

    def test_diff_is_structured_by_section(self) -> None:
        self._publish_v1()
        self.service.derive_draft("stat", "draft-2", self.protocol["protocol_id"], 1)
        changed = copy.deepcopy(self.content)
        changed["stratum_weights"] = {"clear-aisle": "0.35", "cross-traffic": "0.65"}
        changed["admission_rules"][0] = dict(changed["admission_rules"][0])
        changed["admission_rules"][0]["threshold"] = "0.30"
        changed["strata"].append({"key": "night", "label": "夜间", "required_trials": 2})
        changed["metrics"].append({
            "key": "drop_rate", "label": "掉落率", "kind": "continuous",
            "unit": "ratio", "direction": "lower",
        })
        changed["stratum_weights"] = {"clear-aisle": "0.3", "cross-traffic": "0.5", "night": "0.2"}
        self.service.revise_draft("stat", "draft-2", 1, changed)
        result = self.service.diff_draft("draft-2")
        sections = result["diff"]["sections"]
        self.assertEqual([item["stratum"] for item in sections["weights"]["changed"]][0], "clear-aisle")
        self.assertIn("night", {item["key"] for item in sections["strata"]["added"]})
        self.assertIn("drop_rate", {item["key"] for item in sections["metrics"]["added"]})
        changed_rule = sections["rules"]["changed"][0]
        self.assertEqual(changed_rule["metric"], "completed")
        self.assertEqual(changed_rule["fields"]["threshold"], {"from": "0.29", "to": "0.30"})
        self.assertEqual(set(result["diff"]["summary"]), {"meta", "strata", "metrics", "weights", "rules"})

    def test_diff_for_brand_new_draft_treats_everything_as_added(self) -> None:
        draft = self.service.create_draft("stat", "draft-1", self.content)
        result = self.service.diff_draft("draft-1")
        sections = result["diff"]["sections"]
        self.assertIsNone(result["base_version"])
        self.assertEqual({item["key"] for item in sections["strata"]["added"]}, {"clear-aisle", "cross-traffic"})
        self.assertEqual(len(sections["rules"]["added"]), 3)

    # -- 退役 ----------------------------------------------------------------

    def _run_batch_to_decision(self, version: int = 1) -> None:
        self.service.create_batch("operator", "batch-a", self.protocol["protocol_id"], version, "build-a")
        self.service.start_batch("operator", "batch-a", 1)
        rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.service.import_observations("operator", "batch-a", "key-1", rows)
        self.service.seal_batch("stat", "batch-a", 2)
        job = self.service.claim_job("worker", 30)
        analysis = self.service.complete_job("worker", job["job_id"], "stat")
        self.service.decide("approver", "batch-a", analysis["analysis_id"], "approved", "满足规则")

    def test_retire_blocked_while_batch_unfinished(self) -> None:
        self._publish_v1()
        self.service.create_batch("operator", "batch-a", self.protocol["protocol_id"], 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)
        with self.assertRaisesRegex(InvalidState, "batch-a"):
            self.service.retire_protocol("stat", self.protocol["protocol_id"], 1, "尝试退役")

    def test_retire_succeeds_when_unreferenced_and_report_still_readable(self) -> None:
        self._publish_v1()
        self._run_batch_to_decision()
        result = self.service.retire_protocol("stat", self.protocol["protocol_id"], 1, "全部批次结束")
        self.assertEqual(result["status"], "retired")
        # 历史报告仍可读取退役版本的内容。
        report = self.service.report("auditor", "batch-a")
        self.assertEqual(report["protocol"]["version"], 1)
        self.assertEqual(report["protocol"]["lifecycle_status"], "retired")
        self.assertEqual(report["protocol"]["retire_reason"], "全部批次结束")
        version = self.service.get_protocol(self.protocol["protocol_id"], 1)
        self.assertEqual(version["status"], "retired")
        self.assertEqual(version["retire_reason"], "全部批次结束")

    def test_retired_version_cannot_start_new_batch(self) -> None:
        self._publish_v1()
        self.service.retire_protocol("stat", self.protocol["protocol_id"], 1, "无占用")
        with self.assertRaises(InvalidState):
            self.service.create_batch("operator", "batch-z", self.protocol["protocol_id"], 1, "build-a")

    def test_retire_requires_reason(self) -> None:
        self._publish_v1()
        with self.assertRaises(ValidationFailed):
            self.service.retire_protocol("stat", self.protocol["protocol_id"], 1, "  ")

    def test_double_retire_is_invalid_state(self) -> None:
        self._publish_v1()
        self.service.retire_protocol("stat", self.protocol["protocol_id"], 1, "首次退役")
        with self.assertRaises(InvalidState):
            self.service.retire_protocol("stat", self.protocol["protocol_id"], 1, "再次退役")

    def test_history_presents_full_trace(self) -> None:
        self._publish_v1()
        self._run_batch_to_decision()
        self.service.retire_protocol("stat", self.protocol["protocol_id"], 1, "生命周期结束")
        history = self.service.protocol_history("auditor", self.protocol["protocol_id"])
        event_types = [event["event_type"] for event in history["events"]]
        self.assertIn("draft.created", event_types)
        self.assertIn("draft.published", event_types)
        self.assertIn("protocol.published", event_types)
        self.assertIn("protocol.retired", event_types)
        # 时间轨迹必须保持发布先于退役。
        self.assertLess(event_types.index("protocol.published"), event_types.index("protocol.retired"))
        self.assertEqual(history["versions"][0]["status"], "retired")
        with self.assertRaises(Forbidden):
            self.service.protocol_history("operator", self.protocol["protocol_id"])


class StorageMigrationTests(unittest.TestCase):
    def test_legacy_v2_database_migrates_columns(self) -> None:
        connection = sqlite3.connect(":memory:", isolation_level=None)
        connection.row_factory = sqlite3.Row
        # 先按当前模式建表，再删除相关表并以旧版（v2）结构重建，模拟升级场景。
        connection.executescript(SCHEMA_SQL)
        connection.execute("DROP TABLE batches")
        connection.execute("DROP TABLE protocol_catalog")
        connection.execute("DROP TABLE protocol_drafts")
        connection.executescript(
            """
            CREATE TABLE protocol_catalog (
                protocol_id TEXT NOT NULL,
                version INTEGER NOT NULL CHECK (version > 0),
                title TEXT NOT NULL,
                task_family TEXT NOT NULL,
                canonical_json TEXT NOT NULL,
                content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
                created_at TEXT NOT NULL,
                PRIMARY KEY (protocol_id, version),
                UNIQUE (content_sha256)
            );
            CREATE TABLE batches (
                batch_id TEXT PRIMARY KEY,
                protocol_id TEXT NOT NULL,
                protocol_version INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO protocol_catalog VALUES ('p1',1,'t','f','{}',?, '2026-01-01')",
            ("a" * 64,),
        )
        initialize(connection)
        summary = inspect_schema(connection)
        self.assertEqual(summary["schema_version"], "3")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(protocol_catalog)")}
        self.assertIn("retire_reason", columns)
        self.assertIn("status", columns)
        # 既有数据默认处于 active 状态。
        row = connection.execute(
            "SELECT status,retire_reason FROM protocol_catalog WHERE protocol_id='p1'"
        ).fetchone()
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["retire_reason"])
        connection.close()

    def test_file_database_persists_draft(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "life.sqlite3"
            connection = connect(path)
            service = TrialService(connection)
            service.create_user("stat", "stat", "statistician")
            content = protocol_content(load_json(ROOT / "fixtures" / "demo_protocol.json"))
            service.create_draft("stat", "draft-1", content)
            connection.close()
            connection = connect(path)
            service = TrialService(connection)
            draft = service.get_draft("draft-1")
            self.assertEqual(draft["draft_revision"], 1)
            self.assertEqual(draft["protocol_id"], "demo-delivery-v1")
            connection.close()


if __name__ == "__main__":
    unittest.main()
