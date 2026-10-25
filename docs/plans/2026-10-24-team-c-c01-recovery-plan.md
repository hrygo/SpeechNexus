# C01 Admin 同标签页恢复计划

> 已批准 C01/C08 目标内的 inline 执行；沿用 TypeScript / React / generated parser / Vitest / Chromium。只修改 C 所有的 Admin 与探针、交接文档。保留累计未提交工作，不 commit/push、发送消息或改服务运行态。

**Goal:** 刷新后仍能核对原任务、按原取消幂等键显式重放，并从已确认事件水位续读；存储不可用不得发出无法恢复的新取消。

**Spec:** [C01/C08](2026-10-23-three-team-delivery-plan.md#43-c-团队应用与体验)、[既有事件交接](2026-10-24-team-c-c01-events-handoff.md)、公开 `Job` / `JobCancellation` / `JobEvent` 与 `Idempotency-Key` 契约。UI 不新增服务端接口。

## 设计与边界

生产宿主注入同源、同标签页的 sessionStorage journal。私有 v1 记录仅含 `version`、`selectedId`、`cursor` 和 `cancellation: { jobId, key } | null`，单记录上限 2048 字符。内容、Job 快照、取消响应、端点和凭据不落盘。严格字段/标识/十进制水位/UUID 校验；未知版本或损坏记录拒绝使用且不覆盖。局部标识限制属于私有存储安全，不替代生成 wire parser。

取消前同步保存原目标/键，刷新把待提交与结果未知统一视为 unknown，绝不自动 POST。收到且核对原目标的取消响应后才清除 pending；清除失败仍保留 unknown，不把接受等同于终态/释放。恢复只展示标识/水位与待核对状态，不加载旧快照、不自动 GET/SSE。

事件必须完整 Job 重读并成功持久化后才推进可恢复水位；410 精确错误清除旧水位。切换所选任务重置该任务水位，不改变另一原任务的 pending 取消。写入前检查原始记录是否仍与 load/save 时一致；其他 owner 改动或存储异常后阻止本会话取消/事件推进，保留读取能力，等待修复与刷新。此检查不宣称跨进程事务保证。

开发 fake 与未注入 journal 的旧 session 保持仅本页行为。生产不降级为可取消的内存模式。关闭标签页、清除站点数据、跨 origin、完整浏览器崩溃恢复不承诺。恢复信息不是身份或服务端隔离。

## Task 1：私有恢复记录

Files: 新增 `web/admin/src/task-center/task-recovery.ts` / `task-recovery.test.ts`。

Interfaces: `TaskRecoveryRecord`；`TaskRecoveryJournal.load(): TaskRecoveryRecord | null` / `save(record): void`；`SessionTaskRecovery(storage: Pick<Storage, 'getItem' | 'setItem'>, key: string)`；`browserTaskRecovery()` 懒访问全局 sessionStorage，异常安全由 session 处理。

- [x] RED→GREEN：空/合法不可变记录，大水位、独立 pending 目标；拒绝内容附加字段/未知版本/非法键或路径/超长/无 selected 水位；读写拒绝与外部修改不覆盖旧记录。
- [x] Run: `pnpm --filter @speechnexus/admin exec vitest run src/task-center/task-recovery.test.ts`。Expected: 全部通过。

## Task 2：会话恢复与失败关闭

Files: `task-center-session.ts`，新增 `task-center-recovery.test.ts`。

Interfaces: constructor `(api, journal?)`；snapshot 增加 `recovery: 'memory' | 'ready' | 'unavailable'` / `recoveryNotice`。其他公开会话方法签名保留。

- [x] RED→GREEN：重建 session 不自动请求；旧 pending 按原键原目标重放；先持久化再 POST；提交中刷新与卸载恢复 unknown；持久化失败不 POST；响应身份错误不清 pending；ack 保存失败不伪成功；外部改动阻断；选择/事件水位恢复、410 清除、事件 GET 失败不推进、旧连接隔离。
- [x] Run: `pnpm --filter @speechnexus/admin test`。Expected: 原测试及新增全部通过。

## Task 3：生产宿主与真实浏览器刷新

Files: `TaskCenterHost.tsx` / `TaskCenter.tsx` 和相关测试；新增 `scripts/dev-env/admin-task-recovery-probe.mjs`；更新计划索引、goal 与新交接。

- [x] RED→GREEN：生产默认 journal，恢复状态/标识/原任务重读可见，存储异常有安全提示；初始不请求，恢复不自动重放；StrictMode 不销毁恢复会话，fake 不误报持久化。
- [x] Chromium production bundle + loopback 合成 HTTP：丢失取消响应→刷新→切换任务→按原键原目标重放；提交中刷新；大水位刷新续读、410 清除再刷新、外部覆盖/读写失败不新 POST；键盘确认与 320px 亮暗布局；无非 loopback 请求、无内容落盘、无 pageerror。
- [x] Web format/lint/typecheck/test/build；契约检查及生成漂移；既有 Admin 事件/任务探针回归。Expected: 全通过或准确报告范围。
- [x] 一次独立只读终审，按严重度处理；累计补丁包含 untracked 文件，校验 SHA/干净基线应用/暂存区/链接，目标保持 active。

## Review Focus

1. 保存前后浏览器中断、响应/ack 持久化失败：同一原键与目标不能丢失或换键；未知不能显示已接受。
2. 多 owner 原始记录改变、损坏/过大记录/拒绝存储：不能覆盖、自动重试或 silently memory fallback。
3. 选中任务与 pending 任务不同、迟到结果与旧 SSE 读取：独立身份不能串写；当前 Job 必须重新校验。
4. 大整数水位、410 清除失败和 GET 失败：仅已保存且已确认的水位可续读。
5. React StrictMode、pagehide/隐藏/unmount；journal 仅 metadata，生产无 fake、无恢复即网络请求。浏览器关闭/真实部署/跨进程原子性不在本单元证据内。
