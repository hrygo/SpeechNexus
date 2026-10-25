# 团队 C：Workspace 实际 HTTP / PG 浏览器联验

核验日期：2026-10-24。对应 C01 #36 / C08 #43，完整 C00–C08 Goal 仍 active，所有父工作包与 Gate 保持未完成。

## 结果与边界

正式 Admin bundle 经 A 的 `create_app(workspaces=WorkspaceService(PostgresWorkspaceReads(...)))`、真实 Workspace 路由和 SQL，读取隔离 PostgreSQL 中的合成共享工作空间。测试代码可导入 A 实现；C 产品没有新增内部依赖或底座写入。

Chromium 156.0.8078.4：1 context / 1 page，103 个工作空间跨 3 页，6 个 GET，0 浏览器写入、出站或页面错误。覆盖 disabled 仍可读取、元信息版本 7、策略版本 `9007199254740993` 精确保留、UTC 微秒、Unicode/HTML 形状名称纯文本展示、列表后 owner 删除引发实际 GET 404 且清空详情、共享 default、终页与 360px 基本焦点/Tab。随后通过真实 HTTP 另验空表与 default 缺失，无虚构记录。

合成行仅由 disposable 测试 owner 准备及删除。读取器使用非 owner 的 SELECT-only 角色，无 schema CREATE 或表写权限。冻结 0003 Workspace 两条 DDL 直接来自 A，不是完整 Alembic 迁移资格。显式依赖注入且没有对象凭据，未启动 MinIO，因此不证明配置后完整 AssetRuntime/S3 生命周期、生产角色全部授权或生产部署接线。

## 固定版本与改动

- 前版 HEAD `2a46c6e2c7e5ea2f9d29b475bb0524c0b344ef21`，#94 merge 后 base=head `4b08c396c0fe8fc43c7e85683e534befab6bb22b`。没有创建 C commit 或 push。
- 旧 334 路径补丁 SHA-256 `a59fd40d05ba6ae7c6b5f2181dc782ceb05b07317b632dbc642b5197870c2c6d` 及 manifest 保存为 `.runtime/team-c/team-c-before-4b08-integration.*`。干净树预演和实际 FF 全字节一致，332 非 README 路径不变，双语 README 双方合并。
- 新增真实浏览器集成测试与 Chromium-only live 探针；原 `admin-workspaces-probe.mjs` 保留合成故障测试职责。`workbench-pg-probe.py --admin-workspaces` 显式启用实际 A Workspace 资格，不改变默认运行范围。
- 公共协议、生成器、源契约、依赖和 C 产品源码不变；仍消费源 0.1.0.dev0、bindings/package 0.1.0。累计包最终 hash / 路径数以 `.runtime/team-c/team-c-current.manifest.json` 和 issue 最终回读为准，含新增文件。

## 本轮验证

| 层 | 实际结果 |
| --- | --- |
| TDD | 294 既有项通过，新测试因 live 探针缺失失败；补齐探针后 297 项通过，无跳过 |
| 隔离 PG / 浏览器 | PostgreSQL 18.6、Unix socket-only，297 passed / 22.14s；包括既有 C 真实 HTTP / PG / fake LLM 会议与删除浏览器，以及本轮 A Workspace 实际 SQL 浏览器 |
| 清理 | 集群停止后独立 `pg_ctl status` 返回 3，无 postmaster.pid |
| Python | unit/contract 3451 passed / 41.31s；format 591、ruff、mypy 437 文件及 7 依赖边界通过 |
| Web / 契约 | format、lint、typecheck、test、build 和 28 源契约/生成漂移共 12 条门禁命令通过；Admin 110 / Workbench 525，生成契约测试另见原始日志 |

运行入口：

```bash
pnpm build
uv run python scripts/dev-env/workbench-pg-probe.py \
  --pg-bin /path/to/postgresql/bin --browser --admin-workspaces \
  --output .runtime/team-c/workspaces-live-pg.json
```

`--pg-bin` 指向已有 PostgreSQL bin 目录；本探针不安装软件、不发现或连接共享实例。环境事实只适用于本轮 Mac；没有启用 coverage，不声明新覆盖率。Node 26.11.1 与 `.nvmrc` 的 26.12.1 差异沿用已知记录，没有升级。

精确日志、截图、guard/权限/请求和停止证据位于 `.runtime/team-c/real-workspaces-4b08/`；本文件及累计 C 改动尚未发布到 main。原 C03/ASR/TTS 合成浏览器证据是上一单元的分层结果，本轮没有重复运行，不扩大为此次实测。

一次 fresh 终审为 Critical 0 / Important 0 / Minor 2，8 个冻结路径 hash 全匹配，无阻断修复或追加复审。两项新 Minor 延期：Tab 后未断言目标焦点，因此结果中的 `basic_focus_tab` 只支持详情焦点和执行 Tab，不证明完整 Tab 顺序；计划的“无 route interception”措辞与仅出站阻断的 `context.route` 不一致，实际同源请求均 continue，没有 mock/fulfill 响应。这不影响真实 GET/SQL 结论。

## 裁决与余项

1. 保留既有分支/ignored 证据，不自动提交或清理：保护 334 路径累计交接；若判断错误需重新分装，原包可恢复。
2. 冻结 Workspace DDL + SELECT-only 角色 + 显式注入：只验证真实 GET/SQL 消费，不启动 MinIO；代价是完整迁移、AssetRuntime/S3 和生产全部授权仍需另验。
3. 旧 334 路径不重复终审：复用既有审查并逐字节核对非单元文件未变；代价是继承旧审可能遗漏的风险，本次不能代表全产品无缺陷。
4. 未执行写入式变异及全部异常清理注入：仅记录 RED 失败清理、正常独立停止和源码路径证据；代价是未触发分支、浏览器超时资源及完整 Tab 顺序仍待验证。

八项既有 Minor 继续：cursor 空/过大措辞、外部 owner 预检确认措辞、两会议共享 Transcript 并发回归、Meeting 非法成功响应、失败 GET 后旧 conflict/useLatest、Meeting 真卸载后 GET、ASR send 失败前计数、TTS done 后缓冲未播完的本轮停止按钮。模型/绑定/预算、管理写路径、全部生产装配与资格、物理设备/网络/跨浏览器完整 a11y/Linux/GPU、完整 C00–C08 和 G0–G5 均未据此关闭。

共十项 Minor（八项既有、两项新增）未宣称修复。实施与终审进度见[本单元计划](2026-10-24-team-c-workspaces-live-plan.md)和本机 ledger；累计包与九 issue 逐字回读以最终 manifest / 本机记录为准。
