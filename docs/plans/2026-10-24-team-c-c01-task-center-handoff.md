---
title: "SpeechNexus C01 Admin 任务中心消费交接"
status: in-progress
version: "0.1.0"
date: 2026-10-24
---

# C01 Admin 任务中心消费交接

交付 C01 首个任务视图与开发 fake 单元；C01 和[团队 C 持续目标](2026-10-24-team-c-goal.md)仍未完成。范围依据[启动卡](2026-10-23-agent-team-startup-briefs.md#4-c-队macbook-工作台与体验)，具体步骤见[本单元计划](2026-10-24-team-c-c01-task-center-plan.md)。

## 交付身份

- 基线/HEAD：`e09abadf87fbc202b25e78b2698bdf9e95764adb`，分支 `codex/team-c-c00-versions`。本轮 fetch 确认 main 暂无新提交；此前 63 个 C 文件逐文件摘要一致后开始实施。
- 工作区交付，无新增 commit/push、远端 Issue 更新或其他团队消息。补丁与基线/逐文件/补丁摘要位于 `.runtime/team-c/team-c-current.patch` 及 `team-c-current.manifest.json`；新增文件纳入补丁，未跨机集成。
- 消费 [public speech OpenAPI](../../contracts/speech-openapi.yaml) `0.1.0.dev0`、A 的 contracts 包 `0.1.0` 的生成 `JobList` / `Job` / `JobCancellation` / `ErrorResponse` validator 与 typed client。没有改动契约、生成流水线、共享组件/锁文件或组合根。

## 已实现行为

- [AdminJobClient](../../web/admin/src/task-center/job-client.ts)：只调用公开列表/详情/取消路由；保留不透明分页游标，校验响应和资源身份，拒绝重复列表身份/不可继续的分页；请求拒绝 redirect，使用本地 15 秒 active-time deadline，POST 失败不自动重试。此时间策略属于 UI 消费端，不修改服务端协议。
- [TaskCenterSession](../../web/admin/src/task-center/task-center-session.ts)：列表和详情各自 generation/abort，切换后拒绝迟到快照；读取失败保留旧快照并标注过时，卸载中止自有请求。只保留一个未决取消意图，固定原 job/key，切换任务后不能把它当作另一任务的新取消；重读与原键重放为分开的显式动作。
- [TaskCenter](../../web/admin/src/task-center/TaskCenter.tsx)：列表/分页、详情重读、空/错误状态、目标确认与键盘焦点、原键恢复；取消接受、任务状态和 `cleanup_state` 分开显示，不把接受或终态显示为资源释放。确认绑定当前详情对象，选择其他任务会丢弃旧确认。
- [开发场景](../../web/admin/src/task-center/dev/TaskCenterDemo.tsx)：正常分页、取消响应丢失、空列表、读取失败、失败任务/隔离资源。明确标注合成数据、共享部署与 contract/planned，不显示假 GPU 指标。显式开发查询 `?dev-scenario=task-center` 才加载，生产构建没有该入口或 fake 代码。

界面沿用现有 Admin 翡翠主题、系统字体和 Token，不引入外部字体、图片或新的依赖。两栏列表/详情在窄屏上下排列。开发 transport 仅使用合成契约样例和内存，不调用原生 fetch、读取用户内容或写入持久数据。

## 验证事实

核验时间：2026-10-24（Asia/Shanghai）。本轮只改 Web、自己的浏览器探针和文档；未重复运行此前同一基线的 607 项 Python/50 项隔离 PG，结果保留在 [C00 交接](2026-10-24-team-c-c00-project-boundary-handoff.md)。

| 命令/检查 | 实际结果与范围 |
| --- | --- |
| `pnpm --filter @speechnexus/admin test` | 4 文件，19 passed；原 3 项 + 新 16 项客户端/会话/页面行为测试 |
| `pnpm test` | admin 19、workbench 102、contracts 105，共 226 passed |
| `pnpm format:check` / `pnpm lint` / `pnpm typecheck` / `pnpm build` | 全部通过；包含设计 Token/能力状态门禁；不是业务后端已接入的证据 |
| `node scripts/dev-env/admin-task-center-probe.mjs --output .runtime/team-c/c01-task-center-probe-final.json --screenshot .runtime/team-c/c01-task-center-final.png` | 真实 Chromium `156.0.8078.4`，5 个合成场景通过；键盘确认、分页、跨选择原键/原目标重放、重读、空/错误/失败状态；320px 亮/暗模式无横向溢出；page error 0、意外 HTTP/外网请求 0；生产页没有演练入口，JS/CSS 未包含 fake 标识 |
| 原始源码不变的 mutation | 独立 Vite pre-transform 删除列表的 generation/abort 防护，现有迟到列表回归失败：1 failed / 7 passed；原文件 SHA 不变 |
| 本地文档与补丁 | 本地链接和 whitespace 检查；补丁在基线副本应用后逐文件摘要核对，最终结果以交付 manifest 为准 |

TDD：客户端/会话测试先因缺失模块失败，页面测试先因缺失组件失败，再实现并观察通过。首轮有一条测试误用契约不允许的 cancelled/quarantined 组合，已改为合法的 failed/quarantined，不放宽 validator。deadline 测试先观察 timeout 调用数为 0，再实现 signal 组合并通过。首次 lint 暴露测试 mock 的 async/类型问题，已修正并复跑通过。

一次独立只读审查未发现 P1/P2/P3 实质问题；审查者独立运行 19 项 Admin 测试和 Admin typecheck，通过。其未重复运行浏览器、build 或 mutation，以上探针证据由主代理实测。图谱 `check_index_coverage` 返回本仓库未索引，结构判断以直接源码检查为依据。

## 后续与限制

1. A04/C01：使用明确部署版本和批准的同源入口接真实列表/详情/取消，再验证 HTTP 错误、鉴权入口和未知恢复；本单元没有启用生产路由或真实管理写入。
2. C01：接 SSE/历史过期恢复、页面刷新后的持久恢复，以及工作台任务中心；现有本页取消键仅在内存保留，不能声称刷新后原键仍可恢复。当前列表/详情为读取时快照，不承诺自动实时更新。
3. A/C/B：补齐能力、绑定/实例、容量/预算、预设/策略/诊断消费者和管理写入 CAS；这些仍未实现，真实 ready 与算力资格由相应证据决定。
4. C08：继续真实网络/服务、完整键盘流程、屏幕阅读器/对比度、目标 Safari/设备和跨环境验证。本轮只证明列出的 Chromium 合成流程，未访问数据库、设备、语音引擎、LLM 或 GPU，不关闭 Gate。
