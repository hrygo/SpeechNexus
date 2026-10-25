---
title: "团队 C C01 Admin 任务事件与正式入口交接"
status: in-progress
date: 2026-10-24
---

# 团队 C C01 Admin 任务事件与正式入口交接

按[本单元计划](2026-10-24-team-c-c01-events-plan.md)，在[原任务中心](2026-10-24-team-c-c01-task-center-handoff.md)上补齐公开 SSE 消费，并接到正式 Admin 页。[完整 C00–C08 目标](2026-10-24-team-c-goal.md)仍 active，本轮不关闭 C01/C08 或 Gate。

## 身份与实现

`base_sha = head_sha = 3380a76c89998b42be29b0c25e9f49443d23e15b`，分支 `codex/team-c-c00-versions`。开始前核对上一 118 文件摘要，fetch 后 main 无新提交。消费 public speech OpenAPI `0.1.0.dev0`、A contracts/bindings `0.1.0` 的 Job/JobEvent/错误等生成校验。未改 canonical、生成器、共享锁、后端、数据库、部署配置；没有提交、推送或外部消息。

- [正式 App](../../web/admin/src/App.tsx)通过 [TaskCenterHost](../../web/admin/src/task-center/TaskCenterHost.tsx)仅消费 `location.origin`；初始不请求 API，用户显式读取/连接，取消继续要求目标确认。页面标明真实服务待联调、共享部署和本页恢复限制，生产构建排除开发演练。
- 宿主持有会话，StrictMode effect 重放不提前销毁。隐藏/pagehide 停事件并保留水位，返回后显式连接；真正卸载取消自有请求。pagehide 不销毁会话，避免恢复页面后不可用；模拟 visibility 单元测试不证明真实后台节流或 BFCache 资格。
- [AdminJobClient](../../web/admin/src/task-center/job-client.ts)消费生成事件路由，携带 `Last-Event-ID`、拒绝 redirect/cache，仅接受精确 200 和正确 SSE MIME。生成 parser 校验后核对 job、frame id/type；错误仅向会话提供安全 status/code，不展示服务端 message/正文/URL。建连期限 15 秒，建立响应后清 timer；正常长连接由 owner signal 终止，没有自动重连或 POST 重试。
- [会话](../../web/admin/src/task-center/task-center-session.ts)一次保留一个选中任务、连接和已确认水位。事件仅使快照失效，串行重读完整 Job 并核对身份后才推进水位；BigInt 比较避免大序号舍入，重复/旧事件忽略，不拼造完整 Job。取消接受、终态和 cleanup_state 分开呈现。
- EOF/中断需显式重连；只有 410 加 `event_history_expired` 清水位并重读。重读失败标为未恢复，旧快照不冒充当前状态，并禁止基于它取消。切换重置水位，同任务重读保留水位并停旧连接；迟到事件/读取及旧 finally 不影响新 owner。
- 两个入口消费[共享 framing](../../web/shared/src/protocol/sse.ts)，移除原工作台独占实现，原测试导入共享入口。保留严格 UTF-8、CRLF/多行/注释、完整帧终止和 65,536 字符 frame 策略；不把 frame 策略声称为所有传输 chunk 的严格内存总上限。
- 开发页新增水位、历史过期、中断、等待/停止四场景，仅合成内存。事件水位和取消原键仅保留当前页面，刷新会丢失；Admin 持久恢复仍待补齐。

## 验证事实

核验日期 2026-10-24，macOS；Node `v26.11.1`、pnpm `12.14.0`、Chromium `156.0.8078.4`。Node 满足 `>=26 <27`，与 `.nvmrc` `26.12.1` 不完全一致，未升级。

| 检查 | 结果与范围 |
| --- | --- |
| TDD | client 9 项、session 13 项、页面 3 项先失败后通过；宿主先因缺模块失败，2 项实现后通过。206 负例先 1 failed / 9 passed，再要求精确 200 后通过 |
| 完整 Web | Admin 47、Workbench 179、contracts 105，共 331 passed，无跳过；本轮新增 28 项 |
| 静态/构建 | format、lint、typecheck、生产 build 通过；探针独立 format/lint 通过 |
| 原正式契约 | contracts-check 28 文件通过、contracts-drift up-to-date；不代表能力/绑定/realtime 新 validator 已提供 |
| 生产 bundle HTTP/SSE | 实际 loopback 原生 fetch/流、合成服务 6 场景：大水位/重复/重连、410、断线、等待/停止/切换/导航、错误身份、事件后 Job 读取失败；初始 API 请求 0，服务端观察 3 次 owner 关闭 |
| 浏览器演练与回归 | 新内存 4 场景、既有 Admin 5 场景、Workbench 9 场景通过；320px 亮/暗无横向溢出，pageerror/外网请求 0，生产演练代码排除通过 |

```bash
pnpm format:check
pnpm lint
pnpm typecheck
pnpm test
pnpm build
node scripts/dev-env/admin-task-events-probe.mjs --output .runtime/team-c/c01-events-probe-final.json
node scripts/dev-env/admin-task-center-probe.mjs --output .runtime/team-c/c01-events-admin-regression-final.json
node scripts/dev-env/workbench-file-demo-probe.mjs --output .runtime/team-c/c01-events-workbench-regression.json
make contracts-check
make contracts-drift
```

页面测试首次误用 fixture `job_demo` 对已选 `job-demo`，身份防护正确拒绝，修正合成目标后通过。静态检查的 async/yield/unused/ref 提示修正于测试及宿主 owner，没有放宽规则。探针首次将 middleware 对象返回给 Vite，被当后置 hook 调用；改为无返回后重跑通过。失败不计作成功证据。

图谱未索引且当前 analysis 工具面不提供建索引，结构判断限具名源码。未重跑 Python/PG/MinIO、真实语音/设备/GPU 或跨主机；旧证据保留原范围。合成 HTTP 不证明 A04 服务、部署限制、鉴权、TLS、代理缓冲/空闲超时、长时吞吐或 Safari 通过。终审与累计包最终摘要见 `.runtime/team-c/c01-events-progress.md` 和 `team-c-current.manifest.json`。

## 接力与决定代价

1. A04/C01：按指定共同 integration SHA、批准同源入口接真实 HTTP/SSE，验证分页、入口限制、安全错误、持久序列和 410，再查代理缓冲/超时和长连接。
2. C01：补跨刷新取消原键/目标、水位 journal 和工作台任务中心；能力、绑定/实例、预算、预设/策略/诊断继续等待 A 的对应 validator，不以静态类型替代。
3. C08：实际后台/恢复、BFCache、键盘/辅助技术、Safari、真实网络继续取证。C02 下载仍是 proposed，不能以本轮 SSE 替代其 A/C 冻结、生成与接线。

本轮在现有 validator 范围先做事件恢复，代价是其他管理/实时页面仍有 A 依赖；抽出共享 framing，代价是两个入口均须回归；显式单连接、权威重读才推进水位，代价是手动重连和每新事件一次 GET。沿用已批准 C01 范围、inline 与手工 ledger/累计包，代价是显式核对范围/摘要/应用/index。接正式同源 UI，代价是仍需真实部署资格，且本页状态不提供刷新恢复。

## 独立终审

一次独立只读审查未发现高置信 P1/P2；独立运行新增 Admin 28 项和 Workbench SSE/client 10 项均通过，两个入口 typecheck 通过，并确认 framing 移动前后字节相同。没有重要项进入修复轮，不追加复审。

一项 P3 延后：开发场景探针只比较 `state.events.length`，如果未来误发列表/详情原生 GET，该断言不能检出；应在后续验收比较全部 `httpRequests` 或拦截开发页 `/v1/`。当前 fake 源码未调用原生 fetch，因此没有确认的产品请求泄漏。不能把这项探针当作全部开发 HTTP 请求均被计数的证明。

审查未独立重跑浏览器，主代理报告只证明具名合成场景。真实 A04、代理缓存/缓冲/超时、TLS、部署出站限制，以及 Safari/BFCache/后台节流/长时负载继续待验；若误将当前结果当上述资格，真实传输、恢复或资源生命周期可能失败。图谱未索引，结论限具名源码；未重审其他 C00/C02 累计工作，其证据保留原范围，不能新增数据库或内容闭环资格。
