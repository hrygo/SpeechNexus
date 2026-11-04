# C02 正式文件转写宿主实施计划

已授权 C02/C08，采用 native inline / TDD 和一次独立终审。Spec：[三队 C02/C08](2026-10-23-three-team-delivery-plan.md#43-c-团队应用与体验)、现行 speech-openapi、[素材恢复交接](2026-10-24-team-c-c02-asset-reconcile-handoff.md)。完整 C00–C08 goal 保持 active。

目标：正式 Workbench 消费既有上传、原任务恢复、事件和原始制品下载，实现可重复使用的真实 ASR 页面。正式宿主默认同源、无自动请求，无 fake 数据；真实后端/模型资格仍需联调。

设计沿用 AppShell 与已有 Cobalt/中性色语义 Token、系统中文字体、左侧输入/右侧状态；窄屏纵排，长标识换行，显式 focus。去掉占位能力卡，把操作与恢复事实放在主要内容区。没有新增视觉语言或依赖。

A 尚未发布 ModelList 的运行时 validator（bindings/file contract 只有已发布文件子集），不可手写语义副本。本单元使用用户显式填写的部署模型标识；模型发现/资格展示依赖 A 补齐生成入口，仍是未完成接入项，不把手动填写当最终发现体验。

## Task 1：正式宿主与生命周期

Files：新增 `web/workbench/src/file-workflow/FileTaskHost.tsx`、`FileTaskPanel.tsx`、`file-task.css`、`file-task-policy.ts` 和 `FileTaskHost.test.tsx`；修改 `web/workbench/src/App.tsx` / `App.test.tsx`。

Interfaces：消费 `FileTaskSession` / `SpeechFileClient` / `SessionTaskJournal`，不改变其公共协议。独立 ASR key `speechnexus.workbench.asr-task.v1`，严格拒绝非 ASR 私有版本。无正文/文件名存储。

- [x] RED：正式 App 有上传表单且 mount 无网络/无演练；存储失败/损坏/异类版本 fail closed；真实 session 上传/恢复/固定模型、原键重放、hint 原素材 GET、ready 显式继续、取消目标确认、只读事件重连及卸载清理；下载迟到不写 DOM。新任务仅当前任务终态且无 unknown/busy 时显式确认（终审 P2 补充：首次 413 asset_too_large / 422 asset_empty 明确拒绝也可显式纠正；先前未决/刷新恢复不得使用例外），先核对本标签存储 owner，失败保留原记录，不删除服务端数据。
- [x] GREEN：实现表单与详情，原输入恢复/原键重放、取消双步确认、事件显式连接/停止、原始 JSON 下载 Blob URL 释放；StrictMode 重放不销毁会话，pagehide/hidden 停事件，真 unmount 释放。新建另一任务只清理已完成本地意图并换新键。
- [x] Run `pnpm --filter @speechnexus/workbench test` / typecheck；Expected 全通过。必要的真实 multipart 行为由已有 Node 与下一任务浏览器覆盖。

## Task 2：正式 bundle 浏览器与交接

Files：新增 `scripts/dev-env/workbench-file-host-probe.mjs` 与本单元 handoff；更新目标、计划索引及必要中英 README 状态。

Interfaces：使用 Task1 正式生产入口，不加载开发 transport；loopback 合成 HTTP + Chromium，不接真实后台/模型。

- [x] 构建正式 bundle，探针实际 File multipart/Job/Asset/制品，验证初载/刷新无请求、原键/原 ID/显式取消/事件水位/下载、断言非同源零请求、存储无内容、生产排除 fake、键盘 focus/360px 无横溢、截图；Expected 实际浏览器通过。
- [x] Web format/lint/typecheck/test/build、契约和生成漂移、旧开发文件/配音 UI 及新素材恢复回归；Expected 无回归，报告证据范围。
- [x] 一次独立只读终审；按用户影响分级，Important/Critical 一次 TDD 修复，Minor 记录暂缓；累计包含新增文件、干净基线 apply/逐字节/链接/index 核验；不 commit/push。

## Review Focus

1. 存储不可用/异类记录/已知 owner 改写时不能退回内存或换键提交。
2. 刷新/hint/Operation/事件恢复不能自动变成上传、创建或取消。
3. StrictMode、pagehide、卸载和迟到下载不能留下连接或触发页面副作用。
4. 新任务与取消必须针对当前已确认目标，局部清理不影响服务端原任务或制品。
5. 模型手填不是能力发现；页面合成 HTTP 不授予真实模型、网络、校对/导出和完整 C02 资格。
