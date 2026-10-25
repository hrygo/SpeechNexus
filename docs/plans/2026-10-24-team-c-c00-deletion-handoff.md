---
title: "SpeechNexus C00 项目与稿件删除事务交接"
status: in-progress
version: "0.1.0"
date: 2026-10-24
---

# SpeechNexus C00 项目与稿件删除事务交接

## 身份与适用范围

团队 C / C00 #35、C08 #43；[持续目标](2026-10-24-team-c-goal.md)仍为 active。本单元实现当前仅含 PostgreSQL 内容的项目、稿件删除，未完成 C00/C08 或生产删除恢复资格。

基线为 `f210821d655bd7deb326825d750d83584c3ce159`，分支 `codex/team-c-c00-versions`。本轮从 `b3e0ed340763b827333747d464b68fd7f4981116` 快进到已合入 A01/B00 的 main，保留 C 的未提交文件；重叠的计划索引保留双方条目。未创建提交、推送、发送外部消息或修改共享数据库。本地未提交产物没有独立的 `head_sha`。

实现遵循[删除规范 1.0.0](../architecture/data-deletion.md)、[共享删除机器契约](../../contracts/shared/deletion.schema.json)和 [workbench OpenAPI 0.1.0.dev0](../../contracts/workbench-openapi.yaml)。没有修改契约、生成器、共享依赖、部署文件或 A 的迁移框架。领域对象与公开 DTO 分开；当前生成包尚未提供此删除入口的运行时 validator，本单元仅在测试中消费源 schema，未暴露 HTTP 路由。

## 行为与边界

- `domain/deletion.py` 冻结 owner、目标版本和完整排序依赖，预检有效期 300 秒。普通删除阻断拥有的稿件；级联必须恰好确认目标与全部 cascade 依赖，不允许重复、遗漏、多余或确认保留副本。独立副本保留，外模块引用阻断。
- `application/deletions.py` 先按 workspace/family/key 重放原意图，再检查 plan、过期、版本和依赖。plan 摘要覆盖完整公开投影，测试用独立 RFC 8785 实现核验。接受时在同一短事务写入删除记录、全部 tombstone、outbox 和回执。
- `postgres_deletions.py` 与版本适配器共用父项目→排序稿件的行锁顺序。创建稿件同样锁父项目；锁等待后重新检查 tombstone，防止确认之后建立新子资源。已经 tombstone、尚未物理清理的稿件继续阻断父项目删除，不能被另一计划无声接管。
- 版本查询、历史和幂等回放在 tombstone 后隐藏内容。`postgres_deletion_cleanup.py` 由独立 worker 角色消费一个指定 outbox，锁定后读取最新完成账本；删除工作台正文、全部版本及原始回执引用，保留最小 scoped intent 摘要、tombstone 和删除证据。内容擦除、账本插入与 outbox 完成在同一事务提交；失败全部回滚，同一删除重复或并发执行返回同一结果。
- 原接受回执保持最初的 queued 投影；单独查询删除资源可返回 succeeded/complete 和确切 deleted 集合。当前 worker 只执行一个数据库原子步骤，不含后台轮询、running/blocked/failed 的持久调度，也不支持对象或活动执行清理。

所有表、迁移与查询位于 workbench schema，没有 speech 表读写、内部模块依赖、引擎或 GPU 调用。数据库账本只保存身份、版本、摘要与时间，不含业务正文。

## 迁移接入

现有 `version_schema.py` 是 V1 片段；新增 `deletion_schema.py` 为 V2，必须按序由 owner 迁移事务执行。当前版本适配器读取 tombstone/erased receipts，不能仅部署 V1 代码与表。A 负责把两个片段纳入统一迁移序列、owner 锁、角色与组合根；C 不导入当前位于 speech infrastructure 的迁移实现，也不另造一套全局迁移框架。

应用角色追加 plan/意图/tombstone/outbox，不能擦除内容或删除账本。清理角色只获准删除所属业务表、追加最小 erased 回执和账本、更新 outbox 完成时间；为了 PostgreSQL `FOR UPDATE`，另有 projects/transcripts 的 `UPDATE (id)` 权限。它不拥有 schema，不能 DDL，也不能 DELETE/TRUNCATE tombstone、删除意图或账本。集成时需核对角色没有继承额外权限。

## 实测证据

核验时间：2026-10-24，Asia/Shanghai。macOS、Python 3.14.9、PostgreSQL 18.6、锁定依赖。仅使用合成内容；临时集群关闭 TCP、使用独立私有 Unix socket，测试前检查数据库名、owner、实例标记和空 listen_addresses，退出后核对进程及 PID 文件。没有访问共享 PostgreSQL、MinIO、模型或用户数据。

从仓库根目录复现：

```bash
uv run --locked python scripts/dev-env/workbench-pg-probe.py \
  --pg-bin "$(pg_config --bindir)" --coverage
uv run --locked pytest tests/unit tests/contract -q
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy
uv run --locked lint-imports
make contracts-check
make contracts-drift
pnpm format:check
pnpm lint
pnpm typecheck
pnpm test
pnpm build
```

| 检查 | 本轮结果 |
| --- | --- |
| 隔离 PG 探针 | 49 passed：29 项真实数据库场景、16 项参数边界、4 项事务退出 fake；infrastructure 行与分支合计覆盖率 90.19%，实例已确认停止 |
| Python unit/contract | 428 passed；更新 RFC 8785 测试输入类型后，另行重跑领域/投影 14 passed |
| Python 静态检查 | 169 files formatted；ruff passed；mypy 111 source files；6 项依赖边界 kept、0 broken |
| 机器契约与生成漂移 | 28 文件通过；生成绑定 up to date |
| Web | format、lint、typecheck、build 通过；115 tests（workbench 61、admin 3、contracts 51）通过 |

新增数据库场景覆盖精确确认、依赖新增/版本改变、workspace 限定、重放优先、接受阶段故障回滚、重复接受竞争、父锁等待后的新稿件拒绝、物理擦除、两 worker 幂等、清理权限、擦除后账本失败的完整回滚，以及子删除未完成时父删除被阻断。删除后同键同意图返回 resource_not_found；不同意图保持 idempotency_conflict。

独立只读审查发现一项 P1：回执先查 erased 表再查原表，清理可在两条 SQL 中间搬迁回执，导致旧键重新创建项目。真实 PG 交错测试先复现失败（1 failed、43 passed），随后将两表查询合并为一个 statement，共用 READ COMMITTED 快照。修复后，同意图与不同意图两个交错场景均通过。没有给 cleanup 追加相反锁序的幂等键锁。

`.runtime/team-c/c00-pg-probe.json` 指向隔离集群日志、覆盖率和停止记录。当前完整补丁及文件摘要见 `.runtime/team-c/team-c-current.patch` 和相邻 manifest；打包使用临时 Git index，并对基线重放后逐文件核验。图谱没有本仓库索引，当前工具面不能创建索引，结构判断采用直接源码和审查，未声称图谱验证通过。

## 未完成与接力

1. A/C 接入生成删除 DTO/运行时 validator、HTTP/稳定错误映射、迁移框架与组合根，再开放真实服务。当前 Python 投影通过源 schema 只证明测试样例，不等于生产边界完成。
2. C 补后台 outbox 发现、重试/失败状态、工作台删除交互、列表/游标与其他业务资源。当前 worker 必须显式传入删除 id；失败事务保持待处理，不具备生产轮询与故障策略。
3. 对象区、独立保存副本、会议/有声书和活动执行尚未落地；需按具体资源增加精确 pin 的对象清理与停止证据适配器。领域层的 retain 规则不能代替真实副本恢复验证。
4. A07/C08 建立独立于内容备份的删除账本保管、恢复水位和开放读取前的删除重放屏障。当前同库 ledger 不能证明备份恢复不会复活内容，未做恢复演练。
5. Linux PostgreSQL 16/镜像、多进程/跨主机/HA、真实提交网络故障、对象存储、真实浏览器删除及 GPU 联调均未验证。复用的 C02/C04 历史浏览器证据见各交接文档，本轮未重跑真实浏览器。

下一步先推进 C 可独立实现的恢复消费边界与工作台本地交互；待 A 的具名生成/迁移制品就绪后接生产路径。完整 Goal 和所有 Gate 保持原状态，不以本单元替代全产品验收。
