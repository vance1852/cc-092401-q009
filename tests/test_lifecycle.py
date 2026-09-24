from __future__ import annotations

import json
import sqlite3
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from robot_trials.clock import FrozenClock
from robot_trials.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from robot_trials.jsonio import load_json
from robot_trials.service import TrialService


ROOT = Path(__file__).resolve().parents[1]
OBSERVATION_FIXTURE = ROOT / "fixtures" / "demo_observations.jsonl"


class LifecycleTests(unittest.TestCase):
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
        self.service.register_robot("operator", "robot-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "robot-a", "1.0", "b" * 64)

    def tearDown(self) -> None:
        self.connection.close()

    def _publish_v1(self, draft_id: str = "draft-1") -> dict:
        self.service.create_draft("stat", draft_id, self.protocol, edit_note="初稿")
        return self.service.publish_draft("stat", draft_id, 1, note="发布")

    def _v2_payload(self, **changes: object) -> dict:
        raw = deepcopy(self.protocol)
        raw["version"] = 2
        raw.update(changes)
        return raw

    def _run_batch_to_decision(self, batch_id: str, idempotency_key: str = "k") -> dict:
        rows = [
            json.loads(line)
            for line in OBSERVATION_FIXTURE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.service.start_batch("operator", batch_id, 1)
        self.service.import_observations("operator", batch_id, idempotency_key, rows)
        self.service.seal_batch("stat", batch_id, 2)
        job = self.service.claim_job("w")
        analysis = self.service.complete_job("w", job["job_id"], "stat")
        self.service.decide("approver", batch_id, analysis["analysis_id"], "approved", "ok")
        return analysis

    def test_create_and_publish_first_draft(self) -> None:
        draft = self.service.create_draft("stat", "draft-1", self.protocol)
        self.assertEqual(draft["state"], "open")
        self.assertEqual(draft["revision"], 1)
        self.assertIsNone(draft["base_version"])
        self.assertEqual(len(draft["content_sha256"]), 64)
        published = self.service.publish_draft("stat", "draft-1", 1)
        self.assertEqual(published["version"], 1)
        closed = self.service.get_draft("auditor", "draft-1")
        self.assertEqual(closed["state"], "published")
        self.assertEqual(closed["published_version"], 1)

    def test_first_draft_requires_version_one(self) -> None:
        raw = deepcopy(self.protocol)
        raw["version"] = 3
        self.service.create_draft("stat", "draft-x", raw)
        with self.assertRaisesRegex(InvalidState, "版本不连续"):
            self.service.publish_draft("stat", "draft-x", 1)

    def test_create_draft_rejected_for_existing_family(self) -> None:
        self._publish_v1()
        with self.assertRaisesRegex(InvalidState, "派生"):
            self.service.create_draft("stat", "draft-x", self._v2_payload())

    def test_operator_cannot_manage_drafts(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_draft("operator", "draft-x", self.protocol)

    def test_derived_draft_copies_base_and_advances_version(self) -> None:
        self._publish_v1()
        draft = self.service.derive_draft("stat", "draft-2", self.protocol["protocol_id"], 1)
        self.assertEqual(draft["base_version"], 1)
        self.assertEqual(draft["proposed_version"], 2)
        self.assertEqual(draft["content"]["stratum_weights"], {"clear-aisle": "0.4", "cross-traffic": "0.6"})

    def test_derive_from_missing_version(self) -> None:
        with self.assertRaises(NotFound):
            self.service.derive_draft("stat", "draft-x", "nope", 1)

    def test_concurrent_edit_rejected_by_revision(self) -> None:
        draft = self.service.create_draft("stat", "draft-1", self.protocol)
        first = self.service.update_draft(
            "stat", "draft-1", 1, self._v2_payload(title="第二版"), edit_note="改标题"
        )
        self.assertEqual(first["revision"], 2)
        # 另一位统计负责人拿着过期的修订号 1 提交。
        stale = self._v2_payload(stratum_weights={"clear-aisle": "0.3", "cross-traffic": "0.7"})
        with self.assertRaisesRegex(Conflict, "修订号已过期"):
            self.service.update_draft("stat", "draft-1", 1, stale)
        current = self.service.get_draft("auditor", "draft-1")
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["content"]["title"], "第二版")

    def test_publish_stale_revision_rejected(self) -> None:
        self.service.create_draft("stat", "draft-1", self.protocol)
        self.service.update_draft("stat", "draft-1", 1, self.protocol, edit_note="无实质改动")
        with self.assertRaisesRegex(Conflict, "修订号已过期"):
            self.service.publish_draft("stat", "draft-1", 1)

    def test_diff_partitions_strata_metrics_weights_rules(self) -> None:
        self._publish_v1()
        self.service.derive_draft("stat", "draft-2", self.protocol["protocol_id"], 1)
        raw = self._v2_payload()
        raw["strata"][0]["required_trials"] = 5
        raw["strata"].append({"key": "night", "label": "夜间", "required_trials": 2})
        raw["metrics"][1]["threshold_note"] = "x"  # 契约外字段不进入规范内容
        raw["metrics"].append(
            {"key": "drop_rate", "label": "跌落率", "kind": "binary", "unit": None, "direction": "lower"}
        )
        raw["stratum_weights"] = {"clear-aisle": "0.3", "cross-traffic": "0.5", "night": "0.2"}
        raw["admission_rules"].append(
            {"metric": "drop_rate", "statistic": "wilson_lower", "operator": "lte", "threshold": "0.05"}
        )
        raw["admission_rules"][0]["threshold"] = "0.5"
        self.service.update_draft("stat", "draft-2", 1, raw, edit_note="加夜间分层")
        result = self.service.draft_diff("stat", "draft-2")
        diff = result["diff"]
        self.assertFalse(diff["identical"])
        self.assertEqual([item["key"] for item in diff["strata"]["added"]], ["night"])
        changed_strata = {item["key"]: item["fields"] for item in diff["strata"]["changed"]}
        self.assertEqual(changed_strata["clear-aisle"][0]["field"], "required_trials")
        self.assertEqual(changed_strata["clear-aisle"][0]["to"], 5)
        self.assertEqual([item["key"] for item in diff["metrics"]["added"]], ["drop_rate"])
        changed_weights = {item["stratum_key"]: item for item in diff["weights"] if item["changed"]}
        self.assertEqual(changed_weights["night"]["from"], None)
        self.assertEqual(changed_weights["night"]["to"], "0.2")
        added_rules = diff["rules"]["added"]
        self.assertEqual(added_rules[0]["metric"], "drop_rate")
        changed_rules = diff["rules"]["changed"]
        self.assertEqual(changed_rules[0]["from"], "0.29")
        self.assertEqual(changed_rules[0]["to"], "0.5")

    def test_diff_against_explicit_version_and_identical_content(self) -> None:
        self._publish_v1()
        self.service.derive_draft("stat", "draft-2", self.protocol["protocol_id"], 1)
        # 派生草案默认仅版本号 +1；把版本号改回 1 对比内容应一致（仅版本字段差异）。
        same = deepcopy(self.protocol)
        self.service.update_draft("stat", "draft-2", 1, same, edit_note="对齐")
        diff = self.service.draft_diff("stat", "draft-2", against_version=1)["diff"]
        sections_without_fields = [diff["strata"], diff["metrics"], diff["rules"]]
        self.assertTrue(all(not s["added"] and not s["removed"] and not s["changed"]
                            for s in sections_without_fields))
        self.assertFalse(any(w["changed"] for w in diff["weights"]))

    def test_publish_enforces_version_continuity(self) -> None:
        self._publish_v1()
        self.service.derive_draft("stat", "draft-2", self.protocol["protocol_id"], 1)
        raw = self._v2_payload(version=5)
        self.service.update_draft("stat", "draft-2", 1, raw)
        with self.assertRaisesRegex(InvalidState, "下一版必须是 2"):
            self.service.publish_draft("stat", "draft-2", 2)

    def test_publish_rejects_duplicate_content_digest(self) -> None:
        self._publish_v1()
        # 另一个协议族发布完全相同的内容（协议编号相同则必须改版本，这里用同编号同内容派生）。
        self.service.derive_draft("stat", "draft-2", self.protocol["protocol_id"], 1)
        # 把草案内容改得与 v1 逐字节一致（含版本号）。
        self.service.update_draft("stat", "draft-2", 1, deepcopy(self.protocol))
        with self.assertRaisesRegex(Conflict, "内容摘要"):
            self.service.publish_draft("stat", "draft-2", 2)

    def test_incomplete_rules_rejected_while_editing(self) -> None:
        self._publish_v1()
        self.service.derive_draft("stat", "draft-2", self.protocol["protocol_id"], 1)
        raw = self._v2_payload()
        raw["admission_rules"] = []
        with self.assertRaises(ValidationFailed):
            self.service.update_draft("stat", "draft-2", 1, raw)
        raw = self._v2_payload()
        raw["admission_rules"][0]["metric"] = "ghost"
        with self.assertRaises(ValidationFailed):
            self.service.update_draft("stat", "draft-2", 1, raw)

    def test_full_revision_publish_flow(self) -> None:
        self._publish_v1()
        self.service.derive_draft("stat", "draft-2", self.protocol["protocol_id"], 1)
        raw = self._v2_payload(stratum_weights={"clear-aisle": "0.5", "cross-traffic": "0.5"})
        self.service.update_draft("stat", "draft-2", 1, raw, edit_note="权重各半")
        published = self.service.publish_draft("stat", "draft-2", 2, note="发布 v2")
        self.assertEqual(published["version"], 2)
        listing = self.service.list_protocols("auditor")
        self.assertEqual([item["version"] for item in listing["protocols"]], [1, 2])

    def test_retire_blocked_by_unfinished_batches(self) -> None:
        self._publish_v1()
        self.service.create_batch("operator", "batch-open", self.protocol["protocol_id"], 1, "build-a")
        with self.assertRaisesRegex(InvalidState, "尚未结束"):
            self.service.retire_protocol("stat", self.protocol["protocol_id"], 1, "尝试退役")
        self.assertEqual(self.service.open_batch_references(self.protocol["protocol_id"], 1), ["batch-open"])
        self._run_batch_to_decision("batch-open")
        self.assertEqual(self.service.open_batch_references(self.protocol["protocol_id"], 1), [])
        retired = self.service.retire_protocol("stat", self.protocol["protocol_id"], 1, "被取代")
        self.assertEqual(retired["status"], "retired")

    def test_running_batch_keeps_version_occupied_across_states(self) -> None:
        self._publish_v1()
        self.service.create_batch("operator", "b", self.protocol["protocol_id"], 1, "build-a")
        self.service.start_batch("operator", "b", 1)
        with self.assertRaisesRegex(InvalidState, "尚未结束"):
            self.service.retire_protocol("stat", self.protocol["protocol_id"], 1, "理由")

    def test_retire_requires_reason(self) -> None:
        self._publish_v1()
        with self.assertRaisesRegex(ValidationFailed, "理由"):
            self.service.retire_protocol("stat", self.protocol["protocol_id"], 1, "  ")

    def test_retired_version_keeps_historical_reports_but_blocks_new_batches(self) -> None:
        self._publish_v1()
        self.service.create_batch("operator", "batch-old", self.protocol["protocol_id"], 1, "build-a")
        self._run_batch_to_decision("batch-old")
        self.service.retire_protocol("stat", self.protocol["protocol_id"], 1, "退役")
        with self.assertRaisesRegex(InvalidState, "已退役"):
            self.service.create_batch("operator", "batch-new", self.protocol["protocol_id"], 1, "build-a")
        report = self.service.report("auditor", "batch-old")
        self.assertEqual(report["protocol"]["status"], "retired")
        self.assertEqual(report["decision"]["decision"], "approved")

    def test_discard_draft_requires_reason_and_locks_it(self) -> None:
        self.service.create_draft("stat", "draft-x", self.protocol)
        with self.assertRaises(ValidationFailed):
            self.service.discard_draft("stat", "draft-x", 1, " ")
        self.service.discard_draft("stat", "draft-x", 1, "方法学委员会搁置")
        with self.assertRaisesRegex(InvalidState, "discarded"):
            self.service.update_draft("stat", "draft-x", 1, self.protocol)

    def test_timeline_records_draft_to_publish_and_retire(self) -> None:
        self._publish_v1()
        self.service.derive_draft("stat", "draft-2", self.protocol["protocol_id"], 1)
        self.service.update_draft(
            "stat", "draft-2", 1,
            self._v2_payload(stratum_weights={"clear-aisle": "0.5", "cross-traffic": "0.5"}),
            edit_note="调权重",
        )
        self.service.publish_draft("stat", "draft-2", 2, note="v2")
        self.service.retire_protocol("stat", self.protocol["protocol_id"], 1, "被 v2 取代")
        timeline = self.service.protocol_timeline("auditor", self.protocol["protocol_id"])
        event_types = [event["event_type"] for event in timeline["events"]]
        self.assertEqual(
            event_types,
            [
                "protocol_draft.created",
                "protocol.published",
                "protocol_draft.derived",
                "protocol_draft.revised",
                "protocol.published",
                "protocol.retired",
            ],
        )
        versions = timeline["versions"]
        self.assertEqual(versions[0]["status"], "retired")
        self.assertEqual(versions[1]["status"], "active")
        published_payload = timeline["events"][1]["payload"]
        self.assertIn("diff", published_payload)
        self.assertIn("weights", published_payload["diff"])
        # 草案视图保留每次修订，完整呈现讨论过程。
        draft_two = next(d for d in timeline["drafts"] if d["draft_id"] == "draft-2")
        self.assertEqual([r["revision"] for r in draft_two["revisions"]], [1, 2])

    def test_auditor_can_read_but_not_write(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_draft("auditor", "draft-x", self.protocol)
        self.service.create_draft("stat", "draft-1", self.protocol)
        listing = self.service.list_drafts("auditor")
        self.assertEqual([d["draft_id"] for d in listing["drafts"]], ["draft-1"])


if __name__ == "__main__":
    unittest.main()
