"""完整产品流程的离线验收入口。"""

from __future__ import annotations

import argparse
import copy
import json
import tempfile
from pathlib import Path

from .jsonio import load_json
from .service import TrialService
from .storage import connect, inspect_schema


def run(workspace: Path) -> dict[str, object]:
    fixtures = workspace / "fixtures"
    protocol = load_json(fixtures / "demo_protocol.json")
    observation_rows = [
        json.loads(line)
        for line in (fixtures / "demo_observations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with tempfile.TemporaryDirectory(prefix="robot-trials-") as temporary:
        database = Path(temporary) / "foundation.sqlite3"
        connection = connect(database)
        try:
            service = TrialService(connection)
            service.create_user("operator-1", "测试操作员", "operator")
            service.create_user("stat-1", "统计负责人", "statistician")
            service.create_user("approver-1", "准入审批人", "approver")
            service.create_user("auditor-1", "审计人员", "auditor")
            service.register_robot("operator-1", "robot-a", "A 型人形机器人", "示例厂商")
            service.register_build("operator-1", "build-a1", "robot-a", "1.0.0", "a" * 64)

            # 协议生命周期：创建草案 -> 查看差异 -> 提交发布。
            service.create_draft("stat-1", "draft-1", protocol, edit_note="方法学委员会初稿")
            first_diff = service.draft_diff("stat-1", "draft-1")
            published_v1 = service.publish_draft("stat-1", "draft-1", 1, note="发布首版")

            service.create_batch(
                "operator-1", "batch-demo", protocol["protocol_id"], protocol["version"], "build-a1"
            )
            service.start_batch("operator-1", "batch-demo", 1)
            imported = service.import_observations(
                "operator-1", "batch-demo", "demo-import-1", observation_rows
            )
            service.seal_batch("stat-1", "batch-demo", 2)
            job = service.claim_job("worker-1", lease_seconds=60)
            if job is None:
                raise RuntimeError("未能领取分析任务")
            analysis = service.complete_job("worker-1", job["job_id"], "stat-1")
            decision_value = "approved" if analysis["result"]["conclusion"] == "pass" else "rejected"
            service.decide(
                "approver-1", "batch-demo", analysis["analysis_id"], decision_value, "离线验收决定"
            )

            # 批次结束后：基于 v1 派生修订草案，调整权重并发布 v2。
            service.derive_draft("stat-1", "draft-2", protocol["protocol_id"], 1, edit_note="调整权重")
            revision_payload = copy.deepcopy(protocol)
            revision_payload["version"] = 2
            revision_payload["stratum_weights"] = {"clear-aisle": "0.5", "cross-traffic": "0.5"}
            service.update_draft("stat-1", "draft-2", 1, revision_payload, edit_note="权重各半")
            revision_diff = service.draft_diff("stat-1", "draft-2")
            service.publish_draft("stat-1", "draft-2", 2, note="发布第二版")

            # v1 已无未完成批次引用，带理由退役；历史批次报告仍可读取。
            retired = service.retire_protocol(
                "stat-1", protocol["protocol_id"], 1, "被 v2 取代，无未完成批次"
            )
            report = service.report("auditor-1", "batch-demo")
            timeline = service.protocol_timeline("auditor-1", protocol["protocol_id"])
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "3":
        raise RuntimeError("SQLite 基础结构检查失败")
    if report["protocol"]["status"] != "retired":
        raise RuntimeError("历史报告必须仍能读到已退役协议版本")
    if first_diff["diff"]["identical"]:
        raise RuntimeError("首版草案差异应全部为新增")
    weight_changes = [item for item in revision_diff["diff"]["weights"] if item["changed"]]
    if len(weight_changes) != 2:
        raise RuntimeError("修订差异必须按权重呈现变化")
    event_types = [event["event_type"] for event in timeline["events"]]
    for expected in (
        "protocol_draft.created",
        "protocol.published",
        "protocol_draft.derived",
        "protocol_draft.revised",
        "protocol.retired",
    ):
        if expected not in event_types:
            raise RuntimeError(f"审计轨迹缺少事件: {expected}")
    return {
        "status": "ok",
        "protocol": f"{protocol['protocol_id']}@{protocol['version']}",
        "observation_count": imported["inserted"],
        "analysis_id": analysis["analysis_id"],
        "input_sha256": analysis["input_sha256"],
        "conclusion": analysis["result"]["conclusion"],
        "decision": report["decision"]["decision"],
        "published": [published_v1["version"], 2],
        "retired_version": retired["version"],
        "historical_report_status": report["protocol"]["status"],
        "event_count": len(timeline["events"]),
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行试验数据基础工具的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
