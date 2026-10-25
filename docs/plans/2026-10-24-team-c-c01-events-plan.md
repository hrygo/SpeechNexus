# C01 Admin 任务事件恢复实施计划

> 执行：沿用 C01 已批准范围，使用 executing-plans / test-driven-development 在当前 C 分支 inline 实施，一次独立只读终审。保留已有未提交交付，不 commit/push 或发送外部消息。

**Goal:** 让 Admin 任务详情通过公开 JobEvent 触发权威快照重读，支持水位重连、历史过期和旧连接隔离。

**Architecture:** 将两个入口共同使用的 SSE framing 提取到 `web/shared`，业务 JobEvent 仍只由 A 的生成 parser 校验。Admin client 增加公开事件流，session 一次仅持有一个选中任务的连接和一个已确认水位；事件不直接拼成 Job。页面显式连接/停止，不自动重试。

**Tech Stack:** React / TypeScript / openapi-fetch / generated JobEvent parser / Vitest / Chromium。

**Spec:** [C01](2026-10-23-three-team-delivery-plan.md#43-c-团队应用与体验)、[首轮任务卡](2026-10-23-agent-team-startup-briefs.md#4-c-队macbook-工作台与体验)、[既有交接](2026-10-24-team-c-c01-task-center-handoff.md)、`contracts/speech-openapi.yaml` 的 `streamJobEvents`。

## Global Constraints

- 只消费已发布 `Job` / `JobEvent` / `ErrorResponse` validator；能力/绑定与 realtime validator 未发布，不自建替代。
- 事件水位用十进制字符串和 BigInt 比较；只有相应 Job 重读并校验成功才推进，重复/旧事件不重读。
- 410 且 `event_history_expired` 清水位、重读当前 Job；其他错误不伪报历史过期。
- 切换/手动重读/取消后重读/停止/卸载中止旧连接，迟到事件与读取不得覆盖当前选中项。
- SSE 建连等待上限 15 秒；响应建立后由调用方 signal 持有，不用建连 timer 截断正常长连接。没有自动重连或 POST 重试。
- UI 沿用 Admin 翡翠 Token、系统字体、两栏列表/详情和窄屏折行；用户看到事件连接状态及已确认水位，列表仍明确为快照。

## Review Focus

- 等待第一个字节时停止、切换时旧读取迟到、旧流 finally 不得清理新流。
- 大于 JS 安全整数的水位、重复事件、错误 job/frame 身份不得推进水位。
- 410 与其他错误、重读失败不能混淆；保留的快照不得声称当前实时事实。
- 建连 timeout 不应持续到已建立流；停流必须 cancel reader、解除 listener/timer。
- 开发演练无真实服务访问，生产构建排除 fake；不将事件终态转换成资源释放证据。

## Task 1：共享 framing 与 Admin 事件消费

Files: 新建 `web/shared/src/protocol/sse.ts`；替换工作台旧 framing 文件为共享消费，更新其测试与 `speech-file-client.ts`、`web/shared/src/index.ts`；修改 `web/admin/src/task-center/job-client.ts`；新增 `job-events.test.ts`。

Interfaces: `readSse(source: ReadableStream<Uint8Array>, signal?: AbortSignal)` 保留现有 framing 签名；`TaskApi.events?(id, after?, signal?): AsyncIterable<JobEvent>`；`AdminJobClient.events` 使用生成路由和 parser。`TaskResponseError` 只公开安全 status/code。

- [x] RED：缺失 events 方法；GET/Last-Event-ID/无 POST、有效/非法 payload 与 envelope、410、安全错误、正确 MIME、取消 reader、15 秒建连 timer 释放。
- [x] GREEN：共享原 framing 行为与 Admin 消费实现。移除原工作台独占实现；旧 framing 测试改为导入共享入口。
- [x] 验证：`pnpm --filter @speechnexus/admin exec vitest run src/task-center/job-events.test.ts` 和工作台 SSE/client 测试全部通过。

## Task 2：会话、页面与浏览器演练

Files: 修改 `task-center-session.ts`、`TaskCenter.tsx`、开发 transport/page 与正式 `web/admin/src/App.tsx`；新增 `TaskCenterHost.tsx`、宿主测试、`task-center-events.test.ts` 与页面事件测试，新增 `scripts/dev-env/admin-task-events-probe.mjs`；新增交接并更新计划索引/goal。

Interfaces: session `connectEvents(): Promise<void>` / `stopEvents(): void`；snapshot `stream` 与 `cursor`；`eventsAvailable()` 告知视图是否有可用端口。仅当前选中已读 Job 可以连接。

正式宿主仅使用 `location.origin`，初始不发请求，用户显式读取。StrictMode 重放不能提前销毁会话；隐藏/pagehide 停流并保留水位，真正卸载后取消自有请求。页面说明后端尚待部署联调，不在生产加载合成场景。

- [x] RED→GREEN：事件触发完整 Job 重读，成功后推进水位；重复/旧事件忽略；EOF/失败可显式重连；410 清水位并重读；失败保留旧水位；切换/停止/卸载与迟到结果隔离；不可用端口/未选择不能连接。
- [x] RED→GREEN：页面显示连接/停止/水位和失败/过期恢复，连接/切换不绕过取消确认；使用合成事件/HTTP transport 实现开发场景。
- [x] 验证：完整 Web format/lint/typecheck/test/build 全通过；新探针及既有 Admin/工作台文件探针覆盖共享消费，真实 Chromium 合成流、断开/水位/410/切换/停止与 320px 亮暗布局；原生成规范 contracts-check/drift。
- [x] 一次独立终审，无 P1/P2，一项探针 P3 延后；累计包基线应用/摘要/暂存区核验，goal 继续 active。
