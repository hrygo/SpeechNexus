# C02 文件任务跨刷新恢复实施计划

> 执行：使用 `executing-plans` 在本会话逐项实施，最后一次独立只读审查；不创建提交或推送。

**Goal:** 已上传音频的任务提交、取消和事件水位跨刷新保持原始身份，恢复只读，重放须显式触发，避免创建替代执行。

**Architecture:** 在现有 `FileTaskSession` 注入可选的 `FileTaskJournal`，提交 POST 前同步持久化元数据，响应确认后再保存状态。浏览器宿主提供 `sessionStorage`；库不隐式选择存储，不保存音频、文件名、转写、完整 Job 或错误正文。A 的 `parse(JobCreateRequest)` 校验重建的固定 ASR 命令，显式固定素材所属 workspace。

**Spec:** [三团队计划 C02](2026-10-23-three-team-delivery-plan.md#43-c-团队应用与体验)的刷新恢复/不重复发布要求；当前 [文件流程交接](2026-10-24-team-c-c02-file-flow-handoff.md)。这是一项生产页面的必要前置，不代表文件上传、校对保存或生产 HTTP 已全部完成。

## 全局边界与审查重点

- 不改 A 的生成、组合根、共享依赖或协议文件；工作区保留全部已有 C 改动，使用现有分支。
- 内部恢复记录固定 version=1，最多 8192 字符。只允许模型/asset/workspace/job/operation ID、原请求键、pending 操作和已确认水位。损坏、未知版本、额外字段及存储不可用均关闭提交路径，不覆盖旧记录。
- 初始持久化失败时不得 POST；响应后的保存失败仍保留原键与 pending，恢复后显式原键重放。
- 两个控制器读取同一记录后，旧控制器不得覆盖后来写入的状态；相同 intent 的字段不可被其他内容替换。
- 创建未知、取消未知、operation 恢复身份、过期事件历史和已有终态分别保留事实；取消接受不等于资源已释放。
- dispose 后迟到结果不得写记录；持久化失败不得泄露内容或误报已确认。

## Task 1：元数据 journal

Files: 新增 `web/workbench/src/file-workflow/task-journal.ts` / `task-journal.test.ts`。

Interfaces: `TaskRecord`、`FileTaskJournal.load(): TaskRecord | null`、`save(record): void`、`SessionTaskJournal(storage: Pick<Storage, 'getItem' | 'setItem'>, key: string)`；纯同步 CAS 防旧写，错误使用无正文的 `TaskJournalError`。

- [x] RED：往返；损坏/超限/未知版本/额外内容关闭；存储访问失败；旧实例写入冲突；不可变 intent 防覆盖；取消必须有 job 身份。
- [x] GREEN：实现白名单元数据验证，重建命令调用 A parser，不保存 wire 响应。

## Task 2：原会话注入恢复

Files: 修改 `file-task-session.ts`；新增 `file-task-persistence.test.ts`。

Interfaces: 构造器 `FileTaskSession(client, journal?)`；新增 `restore()` 只读恢复；已有 start/replay/refresh/cancel/reconnect 保持兼容。

- [x] RED：创建丢响应→销毁→新会话 restore 无 POST→原键/同 workspace/同 payload 重放；取消丢响应同样只作用原 job；operation 身份只读恢复；写入前/后失败；跨实例冲突；dispose 迟到写；cursor 重连/410。
- [x] GREEN：提交前存 intent，响应后存确认；保存 cursor 后才确认事件；恢复不自动创建/取消。已上传前的原始 File 不落盘，上传未知跨刷新另行接入。

## Task 3：浏览器证据与交接

Files: 新增 `scripts/dev-env/workbench-recovery-probe.mjs`、本单元 handoff；更新计划索引及完整 goal 链接。

- [x] Chromium 实测同一 tab 的 sessionStorage 刷新恢复、原键重放及无音频/正文存储；请求使用合成 fixture，禁止外网请求，明确不代表真实底座/GPU。
- [x] Web format/lint/typecheck/test/build；Python/机器契约相关内容未变则复用仍有效证据并记录准确基线。一次独立只读审查，取消 operation 恢复 P2 回归测试 RED→GREEN；32 项相关、251 项完整 Web 测试通过。
- [x] 累计补丁在干净基线 apply-check、apply 与逐文件字节核对；真实 Git index 不变。完整 C00–C08 goal 保持 active。
