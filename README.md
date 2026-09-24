# 人形机器人试验统计准入服务

本仓库是一套已经实现并可直接运行的服务端项目，用于把人形机器人在递送、讲解和灵巧操作等场景中的结构化试验记录转成可复核的统计准入结论。系统以 SQLite 保存机器人、软件构建、不可变协议版本、试验批次、原始观测、排除申请、分析快照、准入决定和审计事件，不依赖机器人设备、图片、音频、视频或外部基础设施。

现有代码按职责分为：

- `api.py`：标准库实现的 HTTP JSON 接口与无网络路由测试边界；
- `service.py`：角色权限、协议草案生命周期、批次状态机、幂等导入、排除复核、任务租约、审批和报告；
- `lifecycle.py`：协议内容规范化、内容摘要与按分层/指标/权重/规则分区的机器可读差异；
- `analysis.py`：分层覆盖、Wilson 区间、描述性统计、确定性 bootstrap 和准入规则；
- `contracts.py`：协议、指标、分层、权重、随机种子和单次观测的数据契约；
- `jsonio.py`：严格 JSON/JSONL 读取、规范化序列化与内容摘要；
- `numeric.py`：不依赖第三方库的描述性统计和 Wilson 区间；
- `storage.py`：完整 SQLite 业务模式、约束、索引、事务辅助与旧版本迁移；
- `clock.py`：生产时钟与可确定性推进的测试时钟；
- `acceptance.py`：贯通建档、草案、发布、导入、封存、分析、审批、退役和报告的离线验收。

系统已经实现以下主流程：协议以草案形式经方法学委员会反复修订后才能发布，修订只能基于已发布版本派生；每次编辑通过草案修订号做乐观并发控制，差异按分层、指标、权重和规则分区呈现；发布时校验版本连续性、内容摘要全局唯一和规则完整性。协议发布后不可原地覆盖；批次按版本从草稿进入运行、封存、分析和决定状态；被运行中或未完成批次引用的版本不得退役，未被占用的版本可带理由退役，历史批次报告仍可读取；观测分片同时受请求幂等键和来源行唯一身份保护；排除请求必须由不同角色复核；分析任务使用 SQLite 租约避免重复执行并支持过期接管；同一输入快照使用固定算法版本和随机种子得到一致结果；分析者与审批人职责分离，报告保留输入摘要、统计规则、协议状态和批次审计链。

## 协议草案生命周期

协议不能再直接一次性发布，必须经过草案：

1. 协议族首版：`POST /protocols/drafts`（`draft_id` + 完整 `protocol`）。
2. 派生修订：`POST /protocols/{protocol_id}/derive`（`draft_id` + `base_version`），内容复制基线版本并自动把版本号推进一版。
3. 并发编辑：`POST /protocols/drafts/{draft_id}/revisions`，必须携带 `expected_revision`；过期修订号返回 `409 conflict`，每次修订都保留在 `protocol_revisions` 中。
4. 查看差异：`POST /protocols/drafts/{draft_id}/diff?against_version={version}`，结果按 `fields`、`strata`、`metrics`、`weights`、`rules` 分区，全部机器可读。
5. 提交发布：`POST /protocols/drafts/{draft_id}/publish`，同样携带 `expected_revision`；发布校验版本连续性（必须是当前最高版本的下一版）、内容摘要全局唯一（含已退役版本）和规则完整性（分层、指标、权重、准入规则非空且规则引用已声明指标）。
6. 退役：`POST /protocols/{protocol_id}/versions/{version}/retire`，必须给出理由；凡被 `draft/running/sealed/analyzing/analyzed` 状态批次引用的版本返回 `409`。已退役版本不能再新建批次，但历史批次报告照常读取。
7. 完整轨迹：`GET /protocols/{protocol_id}/timeline` 聚合版本、全部草案与修订、以及 `protocol_draft.*`/`protocol.published`/`protocol.retired` 审计事件。

以上操作仅统计负责人（`statistician`）可写；审计人员（`auditor`）可读全部草案、差异与轨迹。

## 环境

- Linux
- Python 3.11 或更高版本
- 无需安装第三方 Python 包

如需安装到隔离环境，可在依赖已经准备好的容器中执行：

```bash
python3 -m pip install --no-index --no-deps .
```

## 测试

在 `project/` 目录执行：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

测试使用临时目录和内存数据库，不访问网络，也不依赖常驻服务。

## 构建检查

本项目是纯 Python 源码包，构建检查采用字节码编译：

```bash
python3 -m compileall -q src tests
```

## 无浏览器验收

下面的命令会读取 `fixtures/` 中的协议与观测记录，在临时 SQLite 数据库中完成用户与设备建档、协议草案创建与发布、批次启动、观测导入、批次封存、任务领取、统计分析、准入审批、第二版协议的派生发布、旧版退役以及审计轨迹导出，随后输出一行 JSON 结果：

```bash
PYTHONPATH=src python3 -m robot_trials.acceptance --workspace .
```

成功时退出码为 `0`，输出中的 `status` 为 `ok`。验收过程不会写入仓库，也不需要浏览器或外部服务。

## 启动 HTTP 服务

```bash
PYTHONPATH=src python3 -m robot_trials.api --database robot_trials.sqlite3 --host 127.0.0.1 --port 8080
```

接口使用 `X-Actor-Id` 表示当前操作人，写入观测时还需提供 `Idempotency-Key`。正式使用前应先创建操作员、统计负责人、审批人和审计人员，再登记机器人、软件构建，并通过协议草案生命周期发布协议版本。服务进程可以停止后重新启动，SQLite 中的业务状态、分析任务和租约信息会保留。
