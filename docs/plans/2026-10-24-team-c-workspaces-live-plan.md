# Admin Workspace 实际 HTTP / PG 浏览器联验计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans 内联实施，整个单元一次 fresh 终审。保留未提交工作树与 ignored 证据，不提交、推送或清理。

**Goal:** 正式 Admin bundle 通过 A 的真实 Workspace GET、应用服务和 PostgreSQL 读取器，验证共享工作空间的分页、disabled 状态、精确版本及缺失响应。

**Architecture:** 固定 #94 merge `4b08c396c0fe8fc43c7e85683e534befab6bb22b`。在一次性 socket-only PG 的独立 schema 内执行 A 的冻结 0003 Workspace DDL，写入合成测试行，使用非 owner 的 SELECT 角色与真实 `PostgresWorkspaceReads`。显式向公开 `create_app(workspaces=...)` 注入真实服务，挂载生产 Admin bundle；Node 探针只操作 Chromium，不提供假 HTTP 响应。

**Tech Stack:** 现有 psycopg / FastAPI / uvicorn / Playwright，现有生产生成客户端；无依赖或公共协议变更。

**Spec:** [三队计划](2026-10-23-three-team-delivery-plan.md) C01/C08；[只读消费者计划](2026-10-24-team-c-workspaces-read-plan.md)；`contracts/admin-openapi.yaml` 的 Workspace 与 GET-only 操作。

## Global Constraints

- C 产品不导入 speech 内部实现、不共写底座表；本测试导入 A 真实实现及创建合成测试行仅用于隔离资格验证。
- 拒绝连接非 disposable 标记、非指定数据库/owner 或监听 TCP 的 PG；不访问共享 5432。
- 合成行由测试 owner 准备/删除，浏览器和运行角色只读；不下载/加载模型，不使用私人音频。
- 显式服务注入不启动 MinIO；不宣称完整 AssetRuntime / S3 启停、全部 Alembic 迁移、运行角色全部授权或生产部署资格。
- 完整 C00–C08、真实设备/模型/Linux/GPU 和 G0–G5 均保持未完成。

## Review Focus

1. private DSN 标记、非 owner 权限和 finally 清理在失败时仍有效。
2. 实际 HTTP 处理器和 SQL 没有被 fixture/fake 替代；报告准确限定显式注入范围。
3. 超过默认页大小的真实分页不能漏项/重复；disabled 不是身份隔离。
4. 大整数版本、Unicode 和 HTML 形状名称保持原值并安全展示。
5. 列表后真实删除导致详情 404 时清空当前详情，不制造默认 Workspace 或写请求。

### Task 1: 固定依赖与真实浏览器探针

**Files:** 新建 `tests/integration/test_admin_workspaces_browser.py`、`scripts/dev-env/admin-workspaces-live-probe.mjs`；修改 `scripts/dev-env/workbench-pg-probe.py` 添加显式 `--admin-workspaces`。

**Interfaces:** 复用 `SPEECHNEXUS_WORKBENCH_TEST_DSN` 的严格 disposable guard；Node 接受 `--url`、`--output`，输出 `result.json`。测试使用真实 `WorkspaceService(PostgresWorkspaceReads(pool))` 和 A `create_app`；Node 不提供 HTTP server/route interception。

- [x] 完整干净预演并安全 FF，保留全部 334 路径，双语 README 双方合并及全字节核对。
- [x] 写出测试并观察缺少 live 探针导致 RED；不把环境缺失或跳过算 RED。
- [x] 实现探针和显式入口，GREEN：103 行跨页完整、disabled/版本/UTC、详情后真实删除 404、空列表/缺失 default、零浏览器写/出站/错误和 360px 基本焦点。
- [x] 一次性 PG 探针 `--browser --admin-workspaces`：297 项通过并停止，独立核对无 pid；可移植命令见交接。

### Task 2: 门禁、终审和交接

**Files:** 本计划、对应 handoff、计划索引及必要双语 README；不改 A 产品实现。

- [x] Python format/ruff/mypy/imports/unit-contract、Web 五门禁及契约漂移按影响验证，区分现有证据和新实测。
- [x] 一次 fresh 终审：Critical0/Important0/Minor2，无阻断修复/追加复审；Minor 和审查限制见交接及 ledger。
- [x] 累计补丁干净 apply / 全字节 / index 保护；#35–#43 正文任务和状态更新并逐字回读，Goal active；最终 manifest hash 再同步回读于本机记录。
