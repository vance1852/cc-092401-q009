"""完整产品流程的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .errors import InvalidState
from .jsonio import load_json
from .service import protocol_content
from .service import TrialService
from .storage import connect, inspect_schema


def run(workspace: Path) -> dict[str, object]:
    from .errors import InvalidState

    fixtures = workspace / "fixtures"
    protocol = load_json(fixtures / "demo_protocol.json")
    observation_rows = [
        json.loads(line)
        for line in (fixtures / "demo_observations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    v2_content = protocol_content(protocol)
    v2_content["stratum_weights"] = {"clear-aisle": "0.35", "cross-traffic": "0.65"}
    v2_content["admission_rules"] = [
        dict(rule) for rule in v2_content["admission_rules"]
    ]
    v2_content["admission_rules"][0]["threshold"] = "0.30"
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

            # 1) 从草案发布 v1。
            draft_v1 = service.create_draft("stat-1", "draft-v1", protocol_content(protocol))
            published_v1 = service.publish_draft("stat-1", "draft-v1", draft_v1["draft_revision"])

            # 2) v1 进入运行批次后不得退役。
            service.create_batch(
                "operator-1", "batch-demo", protocol["protocol_id"], published_v1["version"], "build-a1"
            )
            service.start_batch("operator-1", "batch-demo", 1)
            service.import_observations("operator-1", "batch-demo", "demo-import-1", observation_rows)
            retire_blocked = False
            try:
                service.retire_protocol("stat-1", protocol["protocol_id"], 1, "批次仍在运行")
            except InvalidState:
                retire_blocked = True

            # 3) 完成批次。
            service.seal_batch("stat-1", "batch-demo", 2)
            job = service.claim_job("worker-1", lease_seconds=60)
            if job is None:
                raise RuntimeError("未能领取分析任务")
            analysis = service.complete_job("worker-1", job["job_id"], "stat-1")
            decision_value = "approved" if analysis["result"]["conclusion"] == "pass" else "rejected"
            service.decide(
                "approver-1", "batch-demo", analysis["analysis_id"], decision_value, "离线验收决定"
            )

            # 4) 基于 v1 派生修订草案，先修订内容再查看规范化差异，最后发布 v2。
            derived = service.derive_draft(
                "stat-1", "draft-v2", protocol["protocol_id"], published_v1["version"]
            )
            revised = service.revise_draft(
                "stat-1", "draft-v2", derived["draft_revision"], v2_content
            )
            diff = service.diff_draft("draft-v2")
            published_v2 = service.publish_draft("stat-1", "draft-v2", revised["draft_revision"])

            # 5) 批次结束后 v1 不再被占用，可带理由退役；历史报告仍可读取。
            retired = service.retire_protocol(
                "stat-1", protocol["protocol_id"], 1, "已由 v2 取代，全部在途批次结束"
            )
            report = service.report("auditor-1", "batch-demo")
            history = service.protocol_history("auditor-1", protocol["protocol_id"])
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "3":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "protocol": f"{protocol['protocol_id']}@{published_v2['version']}",
        "published_versions": [published_v1["version"], published_v2["version"]],
        "retire_blocked_while_running": retire_blocked,
        "retired": retired,
        "diff_summary": diff["diff"]["summary"],
        "observation_count": len(observation_rows),
        "analysis_id": analysis["analysis_id"],
        "input_sha256": analysis["input_sha256"],
        "conclusion": analysis["result"]["conclusion"],
        "decision": report["decision"]["decision"],
        "report_protocol_version": report["protocol"]["version"],
        "history_event_count": len(history["events"]),
        "event_count": len(report["events"]),
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
