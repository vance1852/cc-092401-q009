from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from robot_trials.api import JsonApplication
from robot_trials.jsonio import load_json
from robot_trials.service import TrialService, protocol_content


ROOT = Path(__file__).resolve().parents[1]


def _post(application: JsonApplication, path: str, payload: dict, actor: str | None = None, key: str | None = None):
    headers = {"Content-Type": "application/json"}
    if actor:
        headers["X-Actor-Id"] = actor
    if key:
        headers["Idempotency-Key"] = key
    return application.handle("POST", path, headers, json.dumps(payload).encode("utf-8"))


def _get(application: JsonApplication, path: str, actor: str | None = None):
    headers = {"X-Actor-Id": actor} if actor else {}
    return application.handle("GET", path, headers)


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = TrialService(self.connection)
        self.app = JsonApplication(self.service)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("approver", "approver"),
            ("auditor", "auditor"),
        ):
            self.app.handle(
                "POST", "/users", {},
                json.dumps({"user_id": user_id, "display_name": user_id, "role": role}).encode(),
            )
        self.content = protocol_content(load_json(ROOT / "fixtures" / "demo_protocol.json"))
        self.pid = self.content["protocol_id"]
        self.service.register_robot("operator", "robot-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "robot-a", "1.0", "b" * 64)

    def tearDown(self) -> None:
        self.connection.close()

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
        created = _post(self.app, "/protocol-drafts", {"draft_id": "d1", "content": self.content}, "stat")
        self.assertEqual(created.status, 201)
        self.assertEqual(created.body["draft_revision"], 1)

        diff = _get(self.app, "/protocol-drafts/d1/diff", "stat")
        self.assertEqual(diff.status, 200)
        self.assertIsNone(diff.body["base_version"])

        published = _post(self.app, "/protocol-drafts/d1/publish", {"expected_revision": 1}, "stat")
        self.assertEqual(published.status, 200)
        self.assertEqual(published.body["version"], 1)

        listing = _get(self.app, f"/protocols/{self.pid}/versions", "stat")
        self.assertEqual(listing.status, 200)
        self.assertEqual([row["version"] for row in listing.body["versions"]], [1])

        detail = _get(self.app, f"/protocols/{self.pid}/versions/1", "stat")
        self.assertEqual(detail.status, 200)
        self.assertEqual(detail.body["status"], "active")

    def test_derive_revise_publish_over_http(self) -> None:
        _post(self.app, "/protocol-drafts", {"draft_id": "d1", "content": self.content}, "stat")
        _post(self.app, "/protocol-drafts/d1/publish", {"expected_revision": 1}, "stat")

        derived = _post(
            self.app,
            f"/protocols/{self.pid}/versions/1/derive-draft",
            {"draft_id": "d2"},
            "stat",
        )
        self.assertEqual(derived.status, 201)
        self.assertEqual(derived.body["base_version"], 1)

        changed = json.loads(json.dumps(self.content))
        changed["stratum_weights"] = {"clear-aisle": "0.35", "cross-traffic": "0.65"}
        revised = _post(
            self.app, "/protocol-drafts/d2/revise",
            {"expected_revision": 1, "content": changed}, "stat",
        )
        self.assertEqual(revised.status, 200)
        self.assertEqual(revised.body["draft_revision"], 2)

        diff = _get(self.app, "/protocol-drafts/d2/diff", "stat")
        self.assertEqual(diff.body["diff"]["summary"]["weights"], 2)

        published = _post(self.app, "/protocol-drafts/d2/publish", {"expected_revision": 2}, "stat")
        self.assertEqual(published.status, 200)
        self.assertEqual(published.body["version"], 2)

    def test_stale_revision_returns_conflict_over_http(self) -> None:
        _post(self.app, "/protocol-drafts", {"draft_id": "d1", "content": self.content}, "stat")
        _post(self.app, "/protocol-drafts/d1/publish", {"expected_revision": 1}, "stat")
        _post(
            self.app, f"/protocols/{self.pid}/versions/1/derive-draft",
            {"draft_id": "d2"}, "stat",
        )
        changed = json.loads(json.dumps(self.content))
        changed["title"] = "新标题"
        _post(self.app, "/protocol-drafts/d2/revise", {"expected_revision": 1, "content": changed}, "stat")
        stale = _post(self.app, "/protocol-drafts/d2/publish", {"expected_revision": 1}, "stat")
        self.assertEqual(stale.status, 409)
        self.assertEqual(stale.body["error"]["code"], "conflict")

    def test_retire_guard_and_history_over_http(self) -> None:
        _post(self.app, "/protocol-drafts", {"draft_id": "d1", "content": self.content}, "stat")
        _post(self.app, "/protocol-drafts/d1/publish", {"expected_revision": 1}, "stat")

        created = _post(
            self.app, "/batches",
            {"batch_id": "b1", "protocol_id": self.pid, "protocol_version": 1, "build_id": "build-a"},
            "operator",
        )
        self.assertEqual(created.status, 201)
        _post(self.app, "/batches/b1/start", {"expected_revision": 1}, "operator")

        blocked = _post(
            self.app, f"/protocols/{self.pid}/versions/1/retire", {"reason": "在途"}, "stat"
        )
        self.assertEqual(blocked.status, 409)

        history = _get(self.app, f"/protocols/{self.pid}/history", "auditor")
        self.assertEqual(history.status, 200)
        event_types = [event["event_type"] for event in history.body["events"]]
        self.assertIn("protocol.published", event_types)

        forbidden = _get(self.app, f"/protocols/{self.pid}/history", "operator")
        self.assertEqual(forbidden.status, 403)


if __name__ == "__main__":
    unittest.main()
