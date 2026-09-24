"""统计准入服务的领域用例。"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .analysis import ALGORITHM_VERSION, analyze
from .clock import SystemClock, isoformat
from .contracts import Observation, Protocol, ValidationError
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .lifecycle import (
    canonical_text,
    diff_protocols,
    normalized_digest,
    normalize_protocol,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "operator": {
        "catalog.write", "batch.create", "batch.start", "observation.import",
        "exclusion.request", "exclusion.revoke",
    },
    "statistician": {
        "protocol.publish", "protocol.draft.write", "protocol.retire",
        "batch.seal", "exclusion.review", "analysis.run",
    },
    "approver": {"decision.write"},
    "auditor": {"report.read", "audit.read"},
}

# 尚未进入 decided 的批次都算作未完成，会占用协议版本。
OPEN_BATCH_STATES = ("draft", "running", "sealed", "analyzing", "analyzed")


class TrialService:
    """在单个 SQLite 连接上提供全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_robot(
        self, actor_id: str, robot_id: str, model_name: str, vendor: str
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO robots(robot_id,model_name,vendor,created_at) VALUES(?,?,?,?)",
                    (robot_id, model_name, vendor, self._now()),
                )
                self._audit("robot", robot_id, "robot.registered", actor_id, {"model_name": model_name})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"机器人已存在: {robot_id}") from exc
        return {"robot_id": robot_id, "model_name": model_name, "vendor": vendor}

    def register_build(
        self, actor_id: str, build_id: str, robot_id: str, version: str, content_sha256: str
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        if len(content_sha256) != 64:
            raise ValidationFailed("构建摘要必须是 64 位 SHA-256")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO builds(build_id,robot_id,version,content_sha256,created_at) VALUES(?,?,?,?,?)",
                    (build_id, robot_id, version, content_sha256.lower(), self._now()),
                )
                self._audit("build", build_id, "build.registered", actor_id, {"robot_id": robot_id, "version": version})
        except sqlite3.IntegrityError as exc:
            raise Conflict("构建编号、版本或摘要冲突") from exc
        return {"build_id": build_id, "robot_id": robot_id, "version": version}

    def _normalize(self, raw: Mapping[str, Any]) -> tuple[dict[str, Any], str, str]:
        try:
            normalized = normalize_protocol(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        return normalized, canonical_text(normalized), normalized_digest(normalized)

    def create_draft(
        self, actor_id: str, draft_id: str, raw: Mapping[str, Any], edit_note: str = ""
    ) -> dict[str, Any]:
        """为协议族创建首版草案（协议族尚无已发布版本时使用）。"""

        self._require(actor_id, "protocol.draft.write")
        normalized, text, digest = self._normalize(raw)
        protocol_id = normalized["protocol_id"]
        proposed_version = normalized["version"]
        try:
            with transaction(self.connection, immediate=True):
                existing_family = self.connection.execute(
                    "SELECT count(*) FROM protocol_catalog WHERE protocol_id=?", (protocol_id,)
                ).fetchone()[0]
                if existing_family:
                    raise InvalidState(
                        f"协议族 {protocol_id} 已有发布版本，修订必须基于已发布版本派生"
                    )
                now = self._now()
                self.connection.execute(
                    "INSERT INTO protocol_drafts(draft_id,protocol_id,base_version,title,state,revision,"
                    "created_by,created_at,updated_by,updated_at) VALUES(?,?,?,?, 'open', 1,?,?,?,?)",
                    (draft_id, protocol_id, None, normalized["title"], actor_id, now, actor_id, now),
                )
                self.connection.execute(
                    "INSERT INTO protocol_revisions(draft_id,revision,proposed_version,canonical_json,"
                    "content_sha256,edit_note,edited_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        draft_id,
                        1,
                        proposed_version,
                        text,
                        digest,
                        edit_note,
                        actor_id,
                        now,
                    ),
                )
                self._audit(
                    "protocol_draft",
                    draft_id,
                    "protocol_draft.created",
                    actor_id,
                    {"protocol_id": protocol_id, "proposed_version": proposed_version, "sha256": digest},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"草案编号已存在: {draft_id}") from exc
        return self._draft_view(self._draft_row(draft_id))

    def derive_draft(
        self, actor_id: str, draft_id: str, protocol_id: str, base_version: int, edit_note: str = ""
    ) -> dict[str, Any]:
        """基于已发布版本派生修订草案，内容复制基线版本并把版本号推进一版。"""

        self._require(actor_id, "protocol.draft.write")
        base_row = self.connection.execute(
            "SELECT canonical_json FROM protocol_catalog WHERE protocol_id=? AND version=?",
            (protocol_id, base_version),
        ).fetchone()
        if base_row is None:
            raise NotFound("基线协议版本不存在")
        base_content = json.loads(base_row["canonical_json"])
        derived = dict(base_content)
        derived["version"] = base_version + 1
        normalized, text, digest = self._normalize(derived)
        try:
            with transaction(self.connection, immediate=True):
                now = self._now()
                self.connection.execute(
                    "INSERT INTO protocol_drafts(draft_id,protocol_id,base_version,title,state,revision,"
                    "created_by,created_at,updated_by,updated_at) VALUES(?,?,?,?,'open',1,?,?,?,?)",
                    (
                        draft_id,
                        protocol_id,
                        base_version,
                        normalized["title"],
                        actor_id,
                        now,
                        actor_id,
                        now,
                    ),
                )
                self.connection.execute(
                    "INSERT INTO protocol_revisions(draft_id,revision,proposed_version,canonical_json,"
                    "content_sha256,edit_note,edited_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        draft_id,
                        1,
                        normalized["version"],
                        text,
                        digest,
                        edit_note,
                        actor_id,
                        now,
                    ),
                )
                self._audit(
                    "protocol_draft",
                    draft_id,
                    "protocol_draft.derived",
                    actor_id,
                    {
                        "protocol_id": protocol_id,
                        "base_version": base_version,
                        "proposed_version": normalized["version"],
                        "sha256": digest,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"草案编号已存在: {draft_id}") from exc
        return self._draft_view(self._draft_row(draft_id))

    def _draft_row(self, draft_id: str, *, expect_open: bool = False) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM protocol_drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"协议草案不存在: {draft_id}")
        if expect_open and row["state"] != "open":
            raise InvalidState(f"草案已{row['state']}，不能继续修改或发布")
        return row

    def _draft_revision(self, draft_id: str, revision: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM protocol_revisions WHERE draft_id=? AND revision=?", (draft_id, revision)
        ).fetchone()
        if row is None:
            raise NotFound("草案修订号不存在")
        return row

    def _draft_view(self, draft_row: sqlite3.Row) -> dict[str, Any]:
        revision_row = self._draft_revision(draft_row["draft_id"], draft_row["revision"])
        return {
            "draft_id": draft_row["draft_id"],
            "protocol_id": draft_row["protocol_id"],
            "base_version": draft_row["base_version"],
            "state": draft_row["state"],
            "revision": draft_row["revision"],
            "proposed_version": revision_row["proposed_version"],
            "title": draft_row["title"],
            "content": json.loads(revision_row["canonical_json"]),
            "content_sha256": revision_row["content_sha256"],
            "created_by": draft_row["created_by"],
            "created_at": draft_row["created_at"],
            "updated_by": draft_row["updated_by"],
            "updated_at": draft_row["updated_at"],
            "published_version": draft_row["published_version"],
            "published_at": draft_row["published_at"],
            "closed_reason": draft_row["closed_reason"],
        }

    def get_draft(self, actor_id: str, draft_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        if user["role"] not in {"statistician", "auditor"}:
            raise Forbidden("当前角色不能查看协议草案")
        return self._draft_view(self._draft_row(draft_id))

    def list_drafts(self, actor_id: str, state: str | None = None) -> dict[str, Any]:
        user = self._user(actor_id)
        if user["role"] not in {"statistician", "auditor"}:
            raise Forbidden("当前角色不能查看协议草案")
        if state is not None and state not in {"open", "published", "discarded"}:
            raise ValidationFailed("未知草案状态")
        if state is None:
            rows = self.connection.execute(
                "SELECT * FROM protocol_drafts ORDER BY protocol_id, draft_id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM protocol_drafts WHERE state=? ORDER BY protocol_id, draft_id", (state,)
            ).fetchall()
        return {"drafts": [self._draft_view(row) for row in rows]}

    def update_draft(
        self,
        actor_id: str,
        draft_id: str,
        expected_revision: int,
        raw: Mapping[str, Any],
        edit_note: str = "",
    ) -> dict[str, Any]:
        """以草案修订号做乐观并发控制：过期修订号拒绝覆盖。"""

        self._require(actor_id, "protocol.draft.write")
        draft_row = self._draft_row(draft_id, expect_open=True)
        normalized, text, digest = self._normalize(raw)
        if normalized["protocol_id"] != draft_row["protocol_id"]:
            raise ValidationFailed("不能改变草案所属协议族")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE protocol_drafts SET revision=revision+1,title=?,updated_by=?,updated_at=? "
                "WHERE draft_id=? AND state='open' AND revision=?",
                (normalized["title"], actor_id, self._now(), draft_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise Conflict("草案修订号已过期，请基于最新修订重新提交")
            new_revision = expected_revision + 1
            now = self._now()
            self.connection.execute(
                "INSERT INTO protocol_revisions(draft_id,revision,proposed_version,canonical_json,"
                "content_sha256,edit_note,edited_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (draft_id, new_revision, normalized["version"], text, digest, edit_note, actor_id, now),
            )
            self._audit(
                "protocol_draft",
                draft_id,
                "protocol_draft.revised",
                actor_id,
                {
                    "revision": new_revision,
                    "from_revision": expected_revision,
                    "proposed_version": normalized["version"],
                    "sha256": digest,
                    "edit_note": edit_note,
                },
            )
        return self._draft_view(self._draft_row(draft_id))

    def discard_draft(
        self, actor_id: str, draft_id: str, expected_revision: int, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "protocol.draft.write")
        if not reason.strip():
            raise ValidationFailed("废弃草案必须给出理由")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE protocol_drafts SET state='discarded',closed_reason=?,updated_by=?,updated_at=? "
                "WHERE draft_id=? AND state='open' AND revision=?",
                (reason.strip(), actor_id, self._now(), draft_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise Conflict("草案状态或修订号已变化")
            self._audit(
                "protocol_draft",
                draft_id,
                "protocol_draft.discarded",
                actor_id,
                {"revision": expected_revision, "reason": reason.strip()},
            )
        return self._draft_view(self._draft_row(draft_id))

    def _latest_published(self, protocol_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM protocol_catalog WHERE protocol_id=? ORDER BY version DESC LIMIT 1",
            (protocol_id,),
        ).fetchone()

    def draft_diff(
        self, actor_id: str, draft_id: str, against_version: int | None = None
    ) -> dict[str, Any]:
        """查看草案相对基线（或指定已发布版本、或最新发布版本）的规范化差异。"""

        user = self._user(actor_id)
        if user["role"] not in {"statistician", "auditor"}:
            raise Forbidden("当前角色不能查看协议差异")
        draft_view = self._draft_view(self._draft_row(draft_id))
        if against_version is None:
            base_row = None
            if draft_view["base_version"] is not None:
                base_row = self.connection.execute(
                    "SELECT canonical_json,content_sha256,status FROM protocol_catalog "
                    "WHERE protocol_id=? AND version=?",
                    (draft_view["protocol_id"], draft_view["base_version"]),
                ).fetchone()
            else:
                base_row = self._latest_published(draft_view["protocol_id"])
        else:
            base_row = self.connection.execute(
                "SELECT canonical_json,content_sha256,status FROM protocol_catalog "
                "WHERE protocol_id=? AND version=?",
                (draft_view["protocol_id"], against_version),
            ).fetchone()
            if base_row is None:
                raise NotFound("对比基线协议版本不存在")
        old_content = None if base_row is None else json.loads(base_row["canonical_json"])
        return {
            "draft_id": draft_id,
            "draft_revision": draft_view["revision"],
            "from": None
            if base_row is None
            else {
                "protocol_id": draft_view["protocol_id"],
                "version": old_content["version"],
                "sha256": base_row["content_sha256"],
                "status": base_row["status"],
            },
            "to": {
                "protocol_id": draft_view["protocol_id"],
                "version": draft_view["proposed_version"],
                "revision": draft_view["revision"],
                "sha256": draft_view["content_sha256"],
            },
            "diff": diff_protocols(old_content, draft_view["content"]),
        }

    def publish_draft(
        self, actor_id: str, draft_id: str, expected_revision: int, note: str = ""
    ) -> dict[str, Any]:
        """提交发布：校验版本连续性、内容摘要唯一性和规则完整性。"""

        self._require(actor_id, "protocol.publish")
        self._draft_row(draft_id, expect_open=True)
        with transaction(self.connection, immediate=True):
            draft_row = self.connection.execute(
                "SELECT * FROM protocol_drafts WHERE draft_id=?", (draft_id,)
            ).fetchone()
            if draft_row is None:
                raise NotFound(f"协议草案不存在: {draft_id}")
            if draft_row["state"] != "open":
                raise InvalidState(f"草案已{draft_row['state']}，不能发布")
            if draft_row["revision"] != expected_revision:
                raise Conflict("草案修订号已过期，请查看差异后重新发布")
            revision_row = self._draft_revision(draft_id, expected_revision)
            content = json.loads(revision_row["canonical_json"])
            digest = revision_row["content_sha256"]

            # 规则完整性：分层、指标、权重与规则在规范化阶段已经过契约校验，
            # 这里再确认准入规则引用的指标仍然存在且阈值有限（规则完整性闸门）。
            metric_keys = {item["key"] for item in content["metrics"]}
            if not content["strata"] or not metric_keys or not content["admission_rules"]:
                raise ValidationFailed("规则完整性校验失败：分层、指标或准入规则为空")
            for index, rule in enumerate(content["admission_rules"]):
                if rule["metric"] not in metric_keys:
                    raise ValidationFailed(
                        f"规则完整性校验失败：admission_rules[{index}] 引用了未声明指标"
                    )
                Decimal(str(rule["threshold"]))

            # 版本连续性：只能发布首版或当前最高版本的下一版（摘要唯一性优先校验，
            # 因为与已发布版本逐字节相同的草案没有讨论版本号的必要）。
            latest = self._latest_published(draft_row["protocol_id"])
            # 内容摘要唯一性（全局，含已退役版本）。
            duplicate = self.connection.execute(
                "SELECT protocol_id,version FROM protocol_catalog WHERE content_sha256=?",
                (digest,),
            ).fetchone()
            if duplicate is not None:
                raise Conflict(
                    f"内容摘要与已发布版本 {duplicate['protocol_id']}@{duplicate['version']} 完全相同，"
                    "禁止重复发布"
                )
            expected_version = 1 if latest is None else latest["version"] + 1
            if content["version"] != expected_version:
                raise InvalidState(
                    f"版本不连续：{draft_row['protocol_id']} 下一版必须是 {expected_version}，"
                    f"草案提议 {content['version']}"
                )

            old_content = None if latest is None else json.loads(latest["canonical_json"])
            diff = diff_protocols(old_content, content)
            now = self._now()
            try:
                self.connection.execute(
                    "INSERT INTO protocol_catalog(protocol_id,version,title,task_family,canonical_json,"
                    "content_sha256,created_at,status) VALUES(?,?,?,?,?,?,?, 'active')",
                    (
                        draft_row["protocol_id"],
                        content["version"],
                        content["title"],
                        content["task_family"],
                        revision_row["canonical_json"],
                        digest,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("协议版本或内容摘要已经存在") from exc
            self.connection.execute(
                "UPDATE protocol_drafts SET state='published',published_version=?,published_at=?,"
                "updated_by=?,updated_at=? WHERE draft_id=?",
                (content["version"], now, actor_id, now, draft_id),
            )
            identity = f"{draft_row['protocol_id']}@{content['version']}"
            self._audit(
                "protocol",
                identity,
                "protocol.published",
                actor_id,
                {
                    "sha256": digest,
                    "draft_id": draft_id,
                    "draft_revision": expected_revision,
                    "note": note,
                    "changes": {
                        "strata_added": len(diff["strata"]["added"]),
                        "strata_removed": len(diff["strata"]["removed"]),
                        "strata_changed": len(diff["strata"]["changed"]),
                        "metrics_added": len(diff["metrics"]["added"]),
                        "metrics_removed": len(diff["metrics"]["removed"]),
                        "metrics_changed": len(diff["metrics"]["changed"]),
                        "weights_changed": len([w for w in diff["weights"] if w["changed"]]),
                        "rules_added": len(diff["rules"]["added"]),
                        "rules_removed": len(diff["rules"]["removed"]),
                        "rules_changed": len(diff["rules"]["changed"]),
                    },
                    "diff": diff,
                },
            )
        return {
            "protocol_id": draft_row["protocol_id"],
            "version": content["version"],
            "sha256": digest,
            "draft_id": draft_id,
            "draft_revision": expected_revision,
        }

    def list_protocols(self, actor_id: str, include_retired: bool = True) -> dict[str, Any]:
        user = self._user(actor_id)
        if user["role"] not in {"statistician", "approver", "auditor"}:
            raise Forbidden("当前角色不能查看协议目录")
        sql = (
            "SELECT protocol_id,version,title,task_family,content_sha256,created_at,status,"
            "retired_at,retired_by,retire_reason FROM protocol_catalog"
        )
        if not include_retired:
            sql += " WHERE status='active'"
        sql += " ORDER BY protocol_id, version"
        rows = self.connection.execute(sql).fetchall()
        return {"protocols": [dict(row) for row in rows]}

    def open_batch_references(self, protocol_id: str, version: int) -> list[str]:
        """返回引用该协议版本且尚未结束的批次编号。"""

        placeholders = ",".join("?" for _ in OPEN_BATCH_STATES)
        rows = self.connection.execute(
            f"SELECT batch_id FROM batches WHERE protocol_id=? AND protocol_version=? "
            f"AND state IN ({placeholders}) ORDER BY batch_id",
            (protocol_id, version, *OPEN_BATCH_STATES),
        ).fetchall()
        return [row["batch_id"] for row in rows]

    def retire_protocol(
        self, actor_id: str, protocol_id: str, version: int, reason: str
    ) -> dict[str, Any]:
        """带理由退役；被运行中或未完成批次引用的版本不得退役。"""

        self._require(actor_id, "protocol.retire")
        if not reason.strip():
            raise ValidationFailed("退役协议必须给出理由")
        row = self.connection.execute(
            "SELECT status FROM protocol_catalog WHERE protocol_id=? AND version=?",
            (protocol_id, version),
        ).fetchone()
        if row is None:
            raise NotFound("协议版本不存在")
        if row["status"] == "retired":
            raise InvalidState("协议版本已经退役")
        blocking = self.open_batch_references(protocol_id, version)
        if blocking:
            raise InvalidState(
                f"协议版本被 {len(blocking)} 个尚未结束的批次引用，不得退役: {blocking}"
            )
        with transaction(self.connection, immediate=True):
            now = self._now()
            cursor = self.connection.execute(
                "UPDATE protocol_catalog SET status='retired',retired_at=?,retired_by=?,retire_reason=? "
                "WHERE protocol_id=? AND version=? AND status='active'",
                (now, actor_id, reason.strip(), protocol_id, version),
            )
            if cursor.rowcount != 1:
                raise InvalidState("协议版本状态已变化")
            identity = f"{protocol_id}@{version}"
            self._audit(
                "protocol",
                identity,
                "protocol.retired",
                actor_id,
                {"reason": reason.strip(), "open_batch_references": []},
            )
        return {
            "protocol_id": protocol_id,
            "version": version,
            "status": "retired",
            "retired_at": now,
            "retired_by": actor_id,
            "retire_reason": reason.strip(),
        }

    def protocol_timeline(self, actor_id: str, protocol_id: str) -> dict[str, Any]:
        """聚合一个协议族从草案、修订、发布到退役的完整轨迹。"""

        user = self._user(actor_id)
        if user["role"] not in {"statistician", "approver", "auditor"}:
            raise Forbidden("当前角色不能查看协议生命周期轨迹")
        versions = self.connection.execute(
            "SELECT version,title,content_sha256,created_at,status,retired_at,retired_by,retire_reason "
            "FROM protocol_catalog WHERE protocol_id=? ORDER BY version",
            (protocol_id,),
        ).fetchall()
        drafts = self.connection.execute(
            "SELECT * FROM protocol_drafts WHERE protocol_id=? ORDER BY created_at, draft_id",
            (protocol_id,),
        ).fetchall()
        draft_views: list[dict[str, Any]] = []
        for draft_row in drafts:
            view = self._draft_view(draft_row)
            revisions = self.connection.execute(
                "SELECT revision,proposed_version,content_sha256,edit_note,edited_by,created_at "
                "FROM protocol_revisions WHERE draft_id=? ORDER BY revision",
                (draft_row["draft_id"],),
            ).fetchall()
            view["revisions"] = [dict(item) for item in revisions]
            draft_views.append(view)
        draft_ids = [draft_row["draft_id"] for draft_row in drafts]
        family_prefix = protocol_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "@%"
        events: list[dict[str, Any]] = []
        if draft_ids:
            placeholders = ",".join("?" for _ in draft_ids)
            event_rows = self.connection.execute(
                f"SELECT event_type,entity_id,actor_id,payload_json,created_at FROM audit_events "
                f"WHERE (entity_type='protocol' AND entity_id LIKE ? ESCAPE '\\') "
                f"OR (entity_type='protocol_draft' AND entity_id IN ({placeholders})) "
                f"ORDER BY event_id",
                (family_prefix, *draft_ids),
            ).fetchall()
        else:
            event_rows = self.connection.execute(
                "SELECT event_type,entity_id,actor_id,payload_json,created_at FROM audit_events "
                "WHERE entity_type='protocol' AND entity_id LIKE ? ESCAPE '\\' ORDER BY event_id",
                (family_prefix,),
            ).fetchall()
        events = [
            {
                "event_type": item["event_type"],
                "entity_id": item["entity_id"],
                "actor_id": item["actor_id"],
                "created_at": item["created_at"],
                "payload": json.loads(item["payload_json"]),
            }
            for item in event_rows
        ]
        return {
            "protocol_id": protocol_id,
            "versions": [dict(row) for row in versions],
            "drafts": draft_views,
            "events": events,
        }

    def _protocol(self, protocol_id: str, version: int) -> tuple[Protocol, str]:
        row = self.connection.execute(
            "SELECT canonical_json,content_sha256,status FROM protocol_catalog "
            "WHERE protocol_id=? AND version=?",
            (protocol_id, version),
        ).fetchone()
        if row is None:
            raise NotFound("协议版本不存在")
        return Protocol.from_dict(json.loads(row["canonical_json"])), row["content_sha256"]

    def create_batch(
        self,
        actor_id: str,
        batch_id: str,
        protocol_id: str,
        protocol_version: int,
        build_id: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "batch.create")
        catalog_row = self.connection.execute(
            "SELECT status FROM protocol_catalog WHERE protocol_id=? AND version=?",
            (protocol_id, protocol_version),
        ).fetchone()
        if catalog_row is None:
            raise NotFound("协议版本不存在")
        if catalog_row["status"] == "retired":
            raise InvalidState("不能基于已退役的协议版本创建批次")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO batches(batch_id,protocol_id,protocol_version,build_id,state,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (batch_id, protocol_id, protocol_version, build_id, "draft", actor_id, self._now()),
                )
                self._audit("batch", batch_id, "batch.created", actor_id, {"build_id": build_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("批次编号冲突或构建不存在") from exc
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return dict(row)

    def start_batch(self, actor_id: str, batch_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "batch.start")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE batches SET state='running',revision=revision+1,started_at=? "
                "WHERE batch_id=? AND state='draft' AND revision=?",
                (self._now(), batch_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("批次不是当前草稿版本")
            self._audit("batch", batch_id, "batch.started", actor_id, {"from_revision": expected_revision})
        return self.get_batch(batch_id)

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM idempotency_keys WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def import_observations(
        self,
        actor_id: str,
        batch_id: str,
        idempotency_key: str,
        raw_rows: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        self._require(actor_id, "observation.import")
        rows = tuple(raw_rows)
        if not rows:
            raise ValidationFailed("观测数组不能为空")
        request_digest = content_digest(rows)
        scope = f"observations:{batch_id}"
        existing = self._idempotent_response(scope, idempotency_key, request_digest)
        if existing is not None:
            return existing
        batch = self.get_batch(batch_id)
        if batch["state"] != "running":
            raise InvalidState("只有运行中的批次可以导入观测")
        protocol, _ = self._protocol(batch["protocol_id"], batch["protocol_version"])
        parsed: list[Observation] = []
        for raw in rows:
            try:
                item = Observation.from_dict(raw, protocol)
            except ValidationError as exc:
                raise ValidationFailed(str(exc)) from exc
            if item.robot_id != self.connection.execute(
                "SELECT robot_id FROM builds WHERE build_id=?", (batch["build_id"],)
            ).fetchone()["robot_id"]:
                raise ValidationFailed("观测机器人与批次构建不一致")
            parsed.append(item)
        response = {"batch_id": batch_id, "inserted": len(parsed), "request_sha256": request_digest}
        try:
            with transaction(self.connection, immediate=True):
                for item, raw in zip(parsed, rows):
                    self.connection.execute(
                        "INSERT INTO observations(batch_id,source_batch,source_row,robot_id,stratum_key,observed_at," 
                        "metrics_json,content_sha256,imported_by,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            batch_id,
                            item.source_batch,
                            item.source_row,
                            item.robot_id,
                            item.stratum_key,
                            item.observed_at,
                            canonical_json({key: format(value, "f") for key, value in item.metrics.items()}),
                            content_digest([raw]),
                            actor_id,
                            self._now(),
                        ),
                    )
                self.connection.execute(
                    "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) VALUES(?,?,?,?,?)",
                    (scope, idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("batch", batch_id, "observations.imported", actor_id, response)
        except sqlite3.IntegrityError as exc:
            raise Conflict("来源行重复或幂等键并发冲突") from exc
        return response

    def request_exclusion(self, actor_id: str, observation_id: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "exclusion.request")
        observation = self.connection.execute(
            "SELECT observation_id,batch_id FROM observations WHERE observation_id=?", (observation_id,)
        ).fetchone()
        if observation is None:
            raise NotFound("观测不存在")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO exclusion_requests(observation_id,status,reason,requested_by,requested_at) "
                    "VALUES(?,?,?,?,?)",
                    (observation_id, "pending", reason, actor_id, self._now()),
                )
                exclusion_id = cursor.lastrowid
                self._audit("observation", str(observation_id), "exclusion.requested", actor_id, {"reason": reason})
        except sqlite3.IntegrityError as exc:
            raise Conflict("该观测已有待处理或生效排除") from exc
        return {"exclusion_id": exclusion_id, "status": "pending"}

    def review_exclusion(
        self, actor_id: str, exclusion_id: int, approve: bool, note: str
    ) -> dict[str, Any]:
        self._require(actor_id, "exclusion.review")
        row = self.connection.execute(
            "SELECT * FROM exclusion_requests WHERE exclusion_id=?", (exclusion_id,)
        ).fetchone()
        if row is None:
            raise NotFound("排除申请不存在")
        if row["status"] != "pending":
            raise InvalidState("排除申请已经处理")
        if row["requested_by"] == actor_id:
            raise Forbidden("申请人不能复核自己的排除申请")
        status = "approved" if approve else "rejected"
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE exclusion_requests SET status=?,reviewed_by=?,reviewed_at=?,review_note=? "
                "WHERE exclusion_id=? AND status='pending'",
                (status, actor_id, self._now(), note, exclusion_id),
            )
            self._audit("exclusion", str(exclusion_id), f"exclusion.{status}", actor_id, {"note": note})
        return {"exclusion_id": exclusion_id, "status": status}

    def revoke_exclusion(self, actor_id: str, exclusion_id: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "exclusion.revoke")
        row = self.connection.execute(
            "SELECT e.*,o.batch_id FROM exclusion_requests e "
            "JOIN observations o ON o.observation_id=e.observation_id WHERE e.exclusion_id=?",
            (exclusion_id,),
        ).fetchone()
        if row is None:
            raise NotFound("排除记录不存在")
        if row["status"] != "approved":
            raise InvalidState("只有已批准的排除可以撤销")
        if row["requested_by"] != actor_id:
            raise Forbidden("只有原申请人可以撤销排除")
        batch = self.get_batch(row["batch_id"])
        if batch["state"] != "running":
            raise InvalidState("批次封存后不能改变排除状态")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE exclusion_requests SET status='revoked',review_note=?,reviewed_at=? "
                "WHERE exclusion_id=? AND status='approved'",
                (reason, self._now(), exclusion_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("排除状态已变化")
            self._audit(
                "observation",
                str(row["observation_id"]),
                "exclusion.revoked",
                actor_id,
                {"exclusion_id": exclusion_id, "reason": reason},
            )
        return {"exclusion_id": exclusion_id, "status": "revoked"}

    def seal_batch(self, actor_id: str, batch_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "batch.seal")
        with transaction(self.connection, immediate=True):
            pending = self.connection.execute(
                "SELECT count(*) FROM exclusion_requests e JOIN observations o ON o.observation_id=e.observation_id "
                "WHERE o.batch_id=? AND e.status='pending'", (batch_id,)
            ).fetchone()[0]
            if pending:
                raise InvalidState("仍有待复核的排除申请")
            cursor = self.connection.execute(
                "UPDATE batches SET state='sealed',revision=revision+1,sealed_at=? "
                "WHERE batch_id=? AND state='running' AND revision=?",
                (self._now(), batch_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("批次状态或版本已变化")
            new_revision = expected_revision + 1
            now = self._now()
            self.connection.execute(
                "INSERT INTO analysis_jobs(batch_id,batch_revision,state,available_at,created_at,updated_at) "
                "VALUES(?,?, 'queued', ?,?,?)",
                (batch_id, new_revision, now, now, now),
            )
            self._audit("batch", batch_id, "batch.sealed", actor_id, {"revision": new_revision})
        return self.get_batch(batch_id)

    def claim_job(self, worker_id: str, lease_seconds: int = 60) -> dict[str, Any] | None:
        if lease_seconds <= 0:
            raise ValidationFailed("租约时长必须大于零")
        now = self._now()
        expires = isoformat(self.clock.now() + timedelta(seconds=lease_seconds))
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT job_id FROM analysis_jobs WHERE "
                "(state='queued' AND available_at<=?) OR (state='leased' AND lease_expires_at<=?) "
                "ORDER BY available_at,job_id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            self.connection.execute(
                "UPDATE analysis_jobs SET state='leased',attempts=attempts+1,lease_owner=?,lease_expires_at=?,updated_at=? "
                "WHERE job_id=?",
                (worker_id, expires, now, row["job_id"]),
            )
            claimed = self.connection.execute("SELECT * FROM analysis_jobs WHERE job_id=?", (row["job_id"],)).fetchone()
        return dict(claimed)

    def _analysis_observations(self, batch_id: str, protocol: Protocol) -> tuple[Observation, ...]:
        rows = self.connection.execute(
            "SELECT o.*,e.reason AS excluded_reason FROM observations o "
            "LEFT JOIN exclusion_requests e ON e.observation_id=o.observation_id AND e.status='approved' "
            "WHERE o.batch_id=? ORDER BY o.observation_id",
            (batch_id,),
        ).fetchall()
        items: list[Observation] = []
        for row in rows:
            metrics = json.loads(row["metrics_json"])
            items.append(Observation(
                source_batch=row["source_batch"],
                source_row=row["source_row"],
                robot_id=row["robot_id"],
                protocol_id=protocol.protocol_id,
                protocol_version=protocol.version,
                stratum_key=row["stratum_key"],
                observed_at=row["observed_at"],
                metrics={key: Decimal(str(value)) for key, value in metrics.items()},
                excluded_reason=row["excluded_reason"],
            ))
        return tuple(items)

    def complete_job(self, worker_id: str, job_id: int, statistician_id: str) -> dict[str, Any]:
        self._require(statistician_id, "analysis.run")
        job = self.connection.execute("SELECT * FROM analysis_jobs WHERE job_id=?", (job_id,)).fetchone()
        if job is None:
            raise NotFound("分析任务不存在")
        if job["state"] != "leased" or job["lease_owner"] != worker_id:
            raise InvalidState("任务未由当前工作进程持有")
        if job["lease_expires_at"] <= self._now():
            raise InvalidState("任务租约已经过期")
        batch = self.get_batch(job["batch_id"])
        protocol, protocol_digest = self._protocol(batch["protocol_id"], batch["protocol_version"])
        observations = self._analysis_observations(batch["batch_id"], protocol)
        snapshot_rows = [
            {
                "source_batch": item.source_batch,
                "source_row": item.source_row,
                "stratum": item.stratum_key,
                "metrics": {key: format(value, "f") for key, value in item.metrics.items()},
                "excluded_reason": item.excluded_reason,
            }
            for item in observations
        ]
        input_digest = content_digest(snapshot_rows)
        result = analyze(protocol, observations)
        with transaction(self.connection, immediate=True):
            existing = self.connection.execute(
                "SELECT analysis_id,result_json FROM analyses WHERE batch_id=? AND batch_revision=? AND input_sha256=?",
                (batch["batch_id"], job["batch_revision"], input_digest),
            ).fetchone()
            if existing is None:
                cursor = self.connection.execute(
                    "INSERT INTO analyses(batch_id,batch_revision,protocol_sha256,input_sha256,algorithm_version,seed," 
                    "result_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        batch["batch_id"], job["batch_revision"], protocol_digest, input_digest,
                        ALGORITHM_VERSION, protocol.seed, canonical_json(result), statistician_id, self._now(),
                    ),
                )
                analysis_id = cursor.lastrowid
            else:
                analysis_id = existing["analysis_id"]
                result = json.loads(existing["result_json"])
            self.connection.execute(
                "UPDATE analysis_jobs SET state='succeeded',lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE job_id=? AND state='leased' AND lease_owner=?",
                (self._now(), job_id, worker_id),
            )
            self.connection.execute(
                "UPDATE batches SET state='analyzed' WHERE batch_id=? AND state IN ('sealed','analyzing')",
                (batch["batch_id"],),
            )
            self._audit(
                "batch",
                batch["batch_id"],
                "analysis.completed",
                statistician_id,
                {"analysis_id": analysis_id, "input_sha256": input_digest},
            )
        return {"analysis_id": analysis_id, "input_sha256": input_digest, "result": result}

    def fail_job(self, worker_id: str, job_id: int, error: str, retry_seconds: int = 0) -> dict[str, Any]:
        available = isoformat(self.clock.now() + timedelta(seconds=retry_seconds))
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE analysis_jobs SET state='queued',available_at=?,lease_owner=NULL,lease_expires_at=NULL," 
                "last_error=?,updated_at=? WHERE job_id=? AND state='leased' AND lease_owner=?",
                (available, error[:1000], self._now(), job_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("任务未由当前工作进程持有")
        return {"job_id": job_id, "state": "queued", "available_at": available}

    def decide(
        self, actor_id: str, batch_id: str, analysis_id: int, decision: str, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "decision.write")
        if decision not in {"needs_more_data", "approved", "rejected"}:
            raise ValidationFailed("未知准入决定")
        analysis_row = self.connection.execute(
            "SELECT * FROM analyses WHERE analysis_id=? AND batch_id=?", (analysis_id, batch_id)
        ).fetchone()
        if analysis_row is None:
            raise NotFound("分析版本不存在")
        if analysis_row["created_by"] == actor_id:
            raise Forbidden("统计负责人不能批准自己的分析")
        batch = self.get_batch(batch_id)
        if batch["state"] != "analyzed" or batch["revision"] != analysis_row["batch_revision"]:
            raise InvalidState("分析不是批次当前可审批版本")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO decisions(batch_id,analysis_id,decision,reason,decided_by,decided_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (batch_id, analysis_id, decision, reason, actor_id, self._now()),
                )
                self.connection.execute("UPDATE batches SET state='decided' WHERE batch_id=?", (batch_id,))
                self._audit(
                    "batch",
                    batch_id,
                    "decision.recorded",
                    actor_id,
                    {"decision_id": cursor.lastrowid, "analysis_id": analysis_id, "decision": decision},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("该分析版本已经形成决定") from exc
        return {"batch_id": batch_id, "analysis_id": analysis_id, "decision": decision}

    def report(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        if user["role"] not in {"statistician", "approver", "auditor"}:
            raise Forbidden("当前角色不能读取完整报告")
        batch = self.get_batch(batch_id)
        protocol, protocol_digest = self._protocol(batch["protocol_id"], batch["protocol_version"])
        catalog_row = self.connection.execute(
            "SELECT status,retired_at,retire_reason FROM protocol_catalog "
            "WHERE protocol_id=? AND version=?",
            (batch["protocol_id"], batch["protocol_version"]),
        ).fetchone()
        analysis_row = self.connection.execute(
            "SELECT * FROM analyses WHERE batch_id=? ORDER BY analysis_id DESC LIMIT 1", (batch_id,)
        ).fetchone()
        decision_row = None
        if analysis_row is not None:
            decision_row = self.connection.execute(
                "SELECT * FROM decisions WHERE analysis_id=?", (analysis_row["analysis_id"],)
            ).fetchone()
        exclusions = self.connection.execute(
            "SELECT e.exclusion_id,e.observation_id,e.status,e.reason,e.requested_by,e.reviewed_by "
            "FROM exclusion_requests e JOIN observations o ON o.observation_id=e.observation_id "
            "WHERE o.batch_id=? ORDER BY e.exclusion_id", (batch_id,)
        ).fetchall()
        events = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM audit_events "
            "WHERE entity_type='batch' AND entity_id=? "
            "ORDER BY event_id", (batch_id,)
        ).fetchall()
        return {
            "batch": batch,
            "protocol": {
                "protocol_id": protocol.protocol_id,
                "version": protocol.version,
                "sha256": protocol_digest,
                "status": catalog_row["status"],
                "retired_at": catalog_row["retired_at"],
                "retire_reason": catalog_row["retire_reason"],
                "seed": protocol.seed,
                "bootstrap_samples": protocol.bootstrap_samples,
            },
            "analysis": None if analysis_row is None else {
                "analysis_id": analysis_row["analysis_id"],
                "input_sha256": analysis_row["input_sha256"],
                "algorithm_version": analysis_row["algorithm_version"],
                "created_by": analysis_row["created_by"],
                "result": json.loads(analysis_row["result_json"]),
            },
            "decision": None if decision_row is None else dict(decision_row),
            "exclusions": [dict(row) for row in exclusions],
            "events": [dict(row) | {"payload": json.loads(row["payload_json"])} for row in events],
        }
