from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from robot_trials.api import JsonApplication
from robot_trials.service import TrialService


ROOT = Path(__file__).resolve().parents[1]


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(TrialService(self.connection))
        self.app.handle(
            "POST", "/users",
            body=json.dumps({"user_id": "stat", "display_name": "统计负责人", "role": "statistician"}).encode(),
        )
        self.app.handle(
            "POST", "/users",
            body=json.dumps({"user_id": "auditor", "display_name": "审计", "role": "auditor"}).encode(),
        )
        self.protocol = json.loads((ROOT / "fixtures" / "demo_protocol.json").read_text(encoding="utf-8"))
        self.headers = {"X-Actor-Id": "stat"}

    def tearDown(self) -> None:
        self.connection.close()

    def _request(self, method: str, path: str, payload: dict | None = None, headers: dict | None = None) -> tuple:
        merged = dict(self.headers)
        if headers:
            merged.update(headers)
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        response = self.app.handle(method, path, merged, body)
        return response.status, response.body

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_user_route(self) -> None:
        payload = json.dumps({"user_id": "u1", "display_name": "操作员", "role": "operator"}).encode()
        response = self.app.handle("POST", "/users", body=payload)
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["role"], "operator")

    def test_draft_lifecycle_over_http(self) -> None:
        status, body = self._request(
            "POST", "/protocols/drafts",
            {"draft_id": "draft-1", "protocol": self.protocol, "edit_note": "初稿"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(body["revision"], 1)

        status, body = self._request("GET", "/protocols/drafts/draft-1", headers={"X-Actor-Id": "auditor"})
        self.assertEqual(status, 200)
        self.assertEqual(body["content"]["version"], 1)

        status, body = self._request("POST", "/protocols/drafts/draft-1/diff")
        self.assertEqual(status, 200)
        self.assertFalse(body["diff"]["identical"])
        self.assertTrue(body["diff"]["weights"][0]["changed"])

        status, body = self._request(
            "POST", "/protocols/drafts/draft-1/publish", {"expected_revision": 1, "note": "发布"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 1)

        status, body = self._request("GET", "/protocols", headers={"X-Actor-Id": "auditor"})
        self.assertEqual(status, 200)
        self.assertEqual([item["status"] for item in body["protocols"]], ["active"])

        # 基于 v1 派生并发布 v2。
        status, body = self._request(
            "POST", f"/protocols/{self.protocol['protocol_id']}/derive",
            {"draft_id": "draft-2", "base_version": 1},
        )
        self.assertEqual(status, 201)
        revision = dict(self.protocol)
        revision["version"] = 2
        revision["stratum_weights"] = {"clear-aisle": "0.5", "cross-traffic": "0.5"}
        status, body = self._request(
            "POST", "/protocols/drafts/draft-2/revisions",
            {"expected_revision": 1, "protocol": revision},
        )
        self.assertEqual(status, 200)
        status, body = self._request(
            "POST", "/protocols/drafts/draft-2/publish", {"expected_revision": 2}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 2)

        # v1 没有任何批次引用，可以退役。
        status, body = self._request(
            "POST", f"/protocols/{self.protocol['protocol_id']}/versions/1/retire",
            {"reason": "被 v2 取代"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "retired")

        status, body = self._request(
            "GET", f"/protocols/{self.protocol['protocol_id']}/timeline",
            headers={"X-Actor-Id": "auditor"},
        )
        self.assertEqual(status, 200)
        self.assertEqual([v["version"] for v in body["versions"]], [1, 2])
        self.assertEqual(body["versions"][0]["status"], "retired")
        event_types = [event["event_type"] for event in body["events"]]
        self.assertEqual(event_types[0], "protocol_draft.created")
        self.assertIn("protocol.retired", event_types)

    def test_stale_revision_conflict_over_http(self) -> None:
        self._request("POST", "/protocols/drafts", {"draft_id": "d", "protocol": self.protocol})
        status, _ = self._request(
            "POST", "/protocols/drafts/d/revisions",
            {"expected_revision": 1, "protocol": self.protocol, "edit_note": "再存一次"},
        )
        self.assertEqual(status, 200)
        status, body = self._request(
            "POST", "/protocols/drafts/d/publish", {"expected_revision": 1}
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "conflict")

    def test_retire_without_reason_rejected_over_http(self) -> None:
        self._request("POST", "/protocols/drafts", {"draft_id": "d", "protocol": self.protocol})
        self._request("POST", "/protocols/drafts/d/publish", {"expected_revision": 1})
        status, body = self._request(
            "POST", f"/protocols/{self.protocol['protocol_id']}/versions/1/retire", {"reason": " "}
        )
        self.assertEqual(status, 422)
        self.assertIn("理由", body["error"]["message"])


if __name__ == "__main__":
    unittest.main()
