# 音色设计/克隆创建任务入口实施计划

> 执行：按既有持续授权，使用 executing-plans 在当前会话内实施；完成整个单元后仅一次 fresh-context 审查。无 commit/push 授权，使用累计补丁交接。

**Goal:** 提供正式工作台的设计/克隆任务输入、材料确认、公开持久 Job 创建与原键恢复，不把创建任务或试听当声音发布。

**Architecture:** 消费现有 `JobCreateRequest` 的 voice_design/voice_clone 与 `Asset`/`Job`/`JobCancellation` 生成 parser，复用 SpeechFileClient。音色独立控制器和标签页 journal；不修改 ASR/TTS 恢复记录、生成器或共享依赖。

**Tech Stack:** TypeScript、React、Vitest、Chromium；base eb299f7。

**Spec:** [三队计划 C03](2026-10-23-three-team-delivery-plan.md)、[产品设计](../speechnexus-product-architecture.md)、`contracts/speech-openapi.yaml` 和 `contracts/voice-resource.schema.json`。

## Global Constraints

- workbench只消费公开协议，不使用speech内部代码/表/引擎。语义由现行生成parser校验，边界类型从生成入口派生。
- 模型/workspace/已有Asset由用户明确填写；不填虚构模型或候选。克隆必须ready的同workspace voice_reference音频，材料用途明确确认。
- 不保存音频；本标签页仅保存已确认原命令文本/固定素材编号和幂等键。输入冻结后不能被当前表单修改，重载不自动POST。
- 未知结果只重放原键/正文；每次写前CAS，存储失败不能写网络。GET当前与原接受快照分层；终态不等于释放。
- 未有独立voice runtime/parser生产者，不接候选、校验、发布、退役、声音复用；继续保留真实服务/模型/设备/G2待验。

## Review Focus

1. 克隆编号或参考文本改变后，旧材料确认不能继续使用；同workspace音频也必须purpose=voice_reference。
2. 创建接受后丢响应，再重载或改变表单不能创建另一项任务；只能原键/正文重放。
3. 取消丢响应、刷新当前结果与存储损坏并发时，未核验的当前状态不能允许下一任务。
4. 任务结果指向draft不代表published；创建/取消响应都不得覆盖较新GET状态或混用其他kind/workspace。
5. 导航卸载、迟到GET及异常服务端消息不能破坏文本、自动重试、泄漏内容或伪造资源释放。

### Task 1: 原命令与材料屏障

**Files:** 新建 `web/workbench/src/voices/voice-command.ts`、`voice-command.test.ts`。
**Interfaces:** `VoiceCommand` 从 FileContracts提取；`voiceCommand(unknown): VoiceCommand`；`checkVoiceReference(command, asset): void`。后续journal/控制器只接受此输出。

- [x] RED：设计Unicode原文/克隆明确确认、模式字段混用、超限/缺失拒绝；同workspace/purpose/ready/audio素材，错误身份全部拒绝。
- [x] GREEN：使用既有parse；本地材料关联只做上下文核对，不复制schema。
- [x] 验证 `pnpm --filter @speechnexus/workbench exec vitest run src/voices/voice-command.test.ts`，预期全部通过。

### Task 2: 持久意图及会话

**Files:** 新建 `voice-session.ts`、`voice-session.test.ts`。
**Interfaces:** `VoiceSession(client, storage)`，`snapshot/subscribe/create/replay/refresh/cancel/next/dispose`；内部闭合version2 journal/CAS（兼容version1）；持久operation_id/显式读取及首次明确拒绝纠正；返回不可变快照。Task1输出形成原命令，Task3直接消费快照。

- [x] RED：保存失败零POST、外部改写屏障、创建丢回执精确重放、重载不自动操作、错误kind/workspace拒绝、取消未知原键、GET失败/迟到结果、终态清理及next屏障。
- [x] GREEN：单写操作锁、独立GET代次/AbortSignal/有界期限；原POST回执不冒充后续GET。首个clone创建前GET素材，原键重放不被后续素材过期阻断。
- [x] 验证 `pnpm --filter @speechnexus/workbench exec vitest run src/voices`，预期全部通过。

### Task 3: 正式入口及整单元交付

**Files:** 新建 `VoiceCreateHost.tsx`及测试、`scripts/dev-env/workbench-voice-create-probe.mjs`；修改`App.tsx`及入口测试；增加交接、索引和Goal指向。

- [x] RED：独立设计/克隆表单、材料变化清除确认、未知冻结/显式恢复、取消二次确认、终态与cleanup分层；正式入口不使用fake或虚构模型。
- [x] GREEN：同源SpeechFileClient；保留访问过页面及未提交文本，隐藏时停止本地请求，后台任务继续。展示固定原意图/Job/draft引用和候选发布未接入提示。
- [x] 生产bundle Chromium合成HTTP覆盖两种模式、实际创建后丢响应/重载/原键、材料错误零POST、取消/终态清理及360px/基本Tab；保存脚本/结果，预期无出站/意外异常。
- [x] Web五门禁、28契约/生成漂移；Python/PG未改则复用原范围证据。一次独立终审后Important/Critical一轮TDD修复，Minor延期。
- [x] 累计补丁干净apply/逐文件字节/index与链接核验，九issue状态和任务最终更新读回；完整Goal保持active。
