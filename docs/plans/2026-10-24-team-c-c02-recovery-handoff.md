---
title: "团队 C C02 文件任务跨刷新恢复交接"
status: active
date: 2026-10-24
---

# 团队 C C02 文件任务跨刷新恢复交接

[本单元计划](2026-10-24-team-c-c02-recovery-plan.md)实现 C02 生产页面所需的恢复基础，完整[团队 C 目标](2026-10-24-team-c-goal.md)仍为 active。生产 App 尚未启用文件提交页面，本单元不是完整文件闭环或底座资格声明。

## 基线与实现

`base_sha` / `head_sha`：`66bbc15b08ac0588f0d9559762e5dfa5ca2b8641`。本轮 fast-forward 接入与 C 文件不重叠的 B 文档/集成测试变更；保留原有 91 个 C 交付文件，未提交或推送。speech/workbench OpenAPI `info.version=0.1.0.dev0`；生成 bindings / fixture `version=0.1.0`，来源内容未变。

新增 `task-journal.ts`：宿主显式注入 `SessionTaskJournal(sessionStorage, key)`，内部 version=1、最多 8192 字符，仅保存模型、asset/workspace/job/operation ID、原创建/取消键、pending 阶段和已确认事件水位。不保存音频、文件名、转写、Job 结果或错误正文；这些记录不建立身份或私有空间。

原 `FileTaskSession` 可选注入 journal。创建/取消 POST 前先同步持久化意图，确认响应后再更新记录；存储失败阻止提交或保留原键/pending。恢复只读，显式 replay 才能重发原请求。ASR 命令统一经 A 的 `parse(JobCreateRequest)` 校验，固定素材所属 workspace、模型、asset 及输出选项，避免刷新后隐式 workspace 变化。v1 命令构造变化必须升级记录版本，旧版本关闭恢复路径。

损坏、未知版本、额外字段、不可能的阶段组合、同一记录的旧控制器写入或 intent 变化均报无正文错误，保留旧记录。取消接受与资源释放分开显示；水位仅在快照读取且恢复记录写入成功后确认。410 与存储失败均有明确状态，dispose 后迟到确认不能更新记录。

取消请求返回 operation 恢复身份时，GET 原 Operation 及关联 Job 只更新可读快照；不会据此确认取消。原取消键、pending、operation ID 和未知状态跨刷新保留，也不会自动重发取消或创建替代任务。

## 本轮验证

核验日期：2026-10-24（Asia/Shanghai），Node 26.11.1 / Chromium 156.0.8078.4。

- TDD：journal 缺失、恢复/提交路径 8 项失败；额外内部状态 3 项失败；410 期间存储写失败用例亦先失败再修复。独立审查发现的取消 operation 恢复问题亦补充回归测试 RED→GREEN。最终 journal 15、持久化 10、原会话 7，共 32 项相关测试通过。
- 完整 Web 测试 251 通过（contracts 105、Admin 19、Workbench 127）。`pnpm format:check` / `pnpm lint` / `pnpm typecheck` / `pnpm build` 通过。
- 变异：绕过 POST 前 journal 保存后，“初始写失败不得 POST”测试失败；已恢复源码，随后完整 Web 与浏览器验证通过。
- Chromium 使用编译后的真实会话/journal/client、真实 `sessionStorage` 和真实 loopback HTTP。服务端只返回合成机器契约 fixture，并故意丢弃首次创建/取消响应。3 次页面刷新后，创建和取消各 2 次 POST 沿用原键，创建 body 与 workspace 完全相同，只形成 1 个执行意图；`9007199254740993` 水位原样恢复。记录 268 字符，无音频或正文；外网请求、页面/handler 错误均为 0，服务已停止。
- 原有文件演练浏览器探针 9 场景重跑通过，包括取消未知、Operation 恢复、事件过期与下载；生产构建排除演练入口，390px 无横向溢出。该探针使用开发 fake，不接真实语音服务。

```bash
node scripts/dev-env/workbench-recovery-probe.mjs --output .runtime/team-c/c02-recovery-browser-final.json
pnpm --filter @speechnexus/workbench test src/file-workflow/task-journal.test.ts src/file-workflow/file-task-persistence.test.ts src/file-workflow/file-task-session.test.ts
```

证据保留在 `.runtime/team-c/c02-recovery-*.log`、`c02-recovery-browser-final.json`、`c02-recovery-progress.md`。Python 和机器契约文件未变；前一单元的 805 unit/contract、102 隔离混合 PG 探针及契约校验/漂移证据仅按原覆盖范围复用，本轮没有宣称运行 B 新增集成测试。

## 执行取舍与未完成项

1. A 生成入口仍缺工作台响应/稿件与实时 parser，继续实现 C 自有的恢复前置；代价是生产页面接入仍待后续，不能用手写 DTO 绕过依赖。
2. 本单元覆盖已上传音频的 job 创建/取消与水位；原始 File 不落盘，上传结果未知时的跨刷新恢复仍未接入。代价是完整 C02 刷新恢复资格尚未满足，生产宿主必须在补齐后才开放流程。sessionStorage 只保证同一 tab 刷新，不承诺关闭 tab 或跨浏览器恢复。
3. 已确认缺少共享 skill helper/reviewer 模板，使用本单元手工 ledger/review package 与一次独立只读审查；代价是需显式核对范围与接口，未提交记录由可恢复补丁保留。
4. 初版浏览器探针误用了不同 job ID 的事件 fixture，生产 identity guard 正确拒绝。探针改为从当前合成 Job 取 ID 与 ASR 结果；代价是该次失败不计通过，未放宽生产校验。
5. 独立只读审查发现一个 P2：取消 operation 恢复读取关联 Job 后错误清除取消未知状态。单次修复保留恢复身份和 pending，回归测试先失败后通过；未发现其他 P1/P2/P3。代价是缺少明确取消确认时继续显示未知，不能把可读取 Job 当作取消确认。审查未覆盖生产页面、完整 C02、上传未知、真实语音、PostgreSQL 或 GPU，保持这些项目待验证。

生产入口/批准的同源 transport、上传未知恢复、校对保存、格式化导出、工作台 HTTP/对象区和真实语音/设备/GPU/恢复联调仍未完成。宿主必须显式注入 journal，并在损坏或存储不可用时阻止开始新意图；没有注入的旧调用仅保留原开发演练行为。本轮不改 A 的生成、公共锁、组合根或部署。

累计补丁为 `.runtime/team-c/team-c-current.patch`，相邻 manifest 保存 base、文件摘要及 SHA256；本单元累计 97 文件，核对干净基线 apply-check、实际 apply、逐文件字节匹配及真实 Git index 不变。旧 91 文件版本另存 `team-c-c00-text-ecd7060.*`，以保留原核验记录。具体检查结果见 manifest 与本单元 ledger；完整 C00–C08 goal 仍为 active。
