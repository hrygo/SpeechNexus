---
title: "SpeechNexus C01 Admin 任务中心首个消费单元"
status: active
version: "0.1.0"
date: 2026-10-24
---

# C01 Admin 任务中心实施计划

依据[团队 C 目标](2026-10-24-team-c-goal.md)、[启动卡](2026-10-23-agent-team-startup-briefs.md#4-c-队macbook-工作台与体验)和[设计框架](../architecture/ui-ux-design-framework-and-tokens.md)，实现 C01 的首个任务列表/详情/取消消费单元。C01 全范围和团队目标继续保持未完成。

## 边界与设计

- 使用 A 发布的 public speech `JobList` / `Job` / `JobCancellation` / `ErrorResponse` validator 和类型客户端；不访问引擎、内部管理端点或数据库，不扩展生成产物。
- 当前确定基线 `e09abadf87fbc202b25e78b2698bdf9e95764adb`。保留现有 C 的 63 文件交付；无新远端提交、无 commit/push 授权。
- Admin 的翡翠主题、系统字体、间距/颜色 Token 沿用现有框架。列表与详情并排，窄屏上下排列；状态、任务身份、资源清理和取消证据分别呈现。无虚构 GPU 指标或能力 ready。
- 只有开发模式且 `dev-scenario=task-center` 时加载合成场景。生产入口保持真实能力外壳；fake 不进入遥测或生产 bundle。
- 网络读取失败保留并标注上次列表快照；切换详情/分页时取消旧 GET，并拒绝迟到结果覆盖新选择。卸载停止所有本页请求。
- 取消需在页面显式确认目标；只保留一个未决取消意图。POST 响应丢失后保留原 job/key，重读与重放分开；切换选择不能改变待重放目标。取消接受不等于任务取消或资源释放。
- 本单元不声称已接入真实服务器、SSE、预算/绑定/预设/CAS 管理或刷新后持久恢复。公开服务就绪后再做部署联调，C01 完整验收仍需这些后续项。

## Task 1：公开客户端与会话状态

文件：`web/admin/src/task-center/job-client.ts`、`task-center-session.ts` 和各自测试。

- [x] 先写并运行失败测试，证明缺失实现；覆盖生成请求、资源身份、列表/详情迟到、失败保留快照、取消原键/原目标、无自动重试、终态与清理分离、卸载。
- [x] 实现 `AdminJobClient` 和 `TaskCenterSession`，只消费生成类型/validator；HTTP redirect 拒绝，请求采用 15 秒 active-time deadline，错误显示不带原始响应内容。
- [x] 运行集中测试与 TypeScript 检查，观察通过；选取缺省/迟到/重放保护做独立进程 mutation 验证。

## Task 2：开发页面与真实浏览器探针

文件：`TaskCenter.tsx` / CSS、`dev/TaskCenterDemo.tsx` / `demo-transport.ts`、`AppEntry.tsx` / `main.tsx`、页面测试，以及 `scripts/dev-env/admin-task-center-probe.mjs`。

- [x] 先写页面行为测试并观察失败，再实现合成任务分页/详情、未知取消、空列表、读取失败和失败任务场景。
- [x] 用显式开发入口挂载；覆盖键盘可达的确认/恢复按钮，窄屏无横向溢出，状态文案不冒充实际服务。
- [x] 运行 Web 格式/lint/类型/测试/构建；用真实 Chromium 操作确认、原键重放、分页、重读与移动窗口；拦截 API/外网请求，验证生产构建排除 fake。
- [x] 对完整本单元做一次独立只读审查；修复实质问题后验证，记录证据和未验证项，更新包含所有新增文件的补丁。已有授权范围内直接推进，不为常规实现重复询问。

## 审查重点

1. 切换选择后取消的迟到响应不能污染新任务或把原键用到其他任务。
2. 用户看到的 job status、cleanup_state 和取消结果分别来自各自权威快照。
3. 旧 GET、失败读取和卸载不使 stale 数据伪装成最新事实。
4. 合成场景只能开发加载；真实浏览器探针不得访问服务或外网，不读取真实业务内容。
5. 保留共享部署与能力三态；不把部分页面交付宣称为 C01/Gate 完成。

执行记录保存在本计划自己的 `.runtime/team-c/c01-task-center-progress.md`。本机 executing-plans 的共享 helper 目录缺失，采用手动 ledger、同样的 RED→GREEN/最终独立审查；不因此提交、删除或覆盖其他计划产物。

实施/测试与最终独立审查已完成，证据和未验证范围见[交接](2026-10-24-team-c-c01-task-center-handoff.md)。补丁应用验证以交付 manifest 为准；本计划不关闭 C01 或持续目标。
