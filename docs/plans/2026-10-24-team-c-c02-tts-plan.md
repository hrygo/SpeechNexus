# C02 文本配音实施计划

> 执行：`executing-plans` 在本会话实施，TDD 与一次独立只读审查；不提交或推送。

**Goal:** 文本上传→固定 voice/profile revision 的 TTS Job→事件读取→合成音频试听/下载，创建/取消及水位跨刷新保持原身份。

**Architecture:** 复用现有 `SpeechFileClient` / `FileTaskSession`，新增显式 `startText(text, selection)`；内部 journal v2 保存文本 asset ID、voice/profile revision，不存正文。v1 ASR 记录仍按原命令恢复。生产入口继续关闭，开发页面仅消费合成 transport。

**Tech Stack:** TypeScript / React / A 的公开 `createSpeechClient`、`parse` / Vitest / Chromium。

**Spec:** [三团队计划 C02](2026-10-23-three-team-delivery-plan.md#43-c-团队应用与体验)；`contracts/speech-openapi.yaml` 的 `TtsJobCreate`、`ResourceReference`、`AssetUploadRequest`。上传未知跨刷新仍待后续，不假称完整 C02。

## 边界与审查重点

- 不修改契约、生成器、共享锁或组合根，不创建第二份 wire validator。只有明确开发参数加载 fake，生产 bundle 排除。
- 固定 model、text asset/workspace、voice/profile ID+revision、WAV 输出；重放不读取后来修改的表单值。
- 原文仅在上传前驻留内存，不存 sessionStorage、URL、日志；v2 仅元数据，精确白名单、CAS 和关闭损坏记录的行为沿用 v1。
- 伪造 ASR/跨 workspace Job 不能确认 TTS；存储写失败不 POST；取消恢复不能由 GET Job 伪确认。
- 试听只在用户点击后创建 Blob URL；卸载/替换停止并撤销；取消不等于资源已释放，Artifact 过期有明确错误。

## Task 1：TTS 固定意图与 journal

Files: `task-journal.ts`；新增 `tts-task.test.ts`。

Interfaces: `TtsSelection {model,voiceId,voiceRevision,profileId,profileRevision}`；`TaskRecord` 为 v1 ASR / v2 TTS 元数据联合；`jobCommand(record)` 唯一重建命令并经 A parser；`SessionTaskJournal` 原接口不变。

- [x] RED：v2 往返、拒绝 voice/profile revision 非法与改写、无正文；v1 原命令保持；未知版本关闭。
- [x] GREEN：v2 命令重建，仍复用既有 journal 的阶段/CAS/大小/生命周期保护。

## Task 2：文本提交与恢复

Files: `speech-file-client.ts` / `file-task-session.ts`；新增 `tts-task.test.ts`。

Interfaces: `uploadText(text,key,signal?)` 真实 multipart `purpose=text_input`；`startText(text,selection)`；现有 restore/replay/cancel/reconnect 不变。

- [x] RED：UTF-8 multipart；丢创建响应→新会话原键/原 asset/workspace/voice/profile 重放；表单变更不改变原意图；无效选择/异类 Job/写失败关闭；取消未知与 SSE 水位。
- [x] GREEN：文本上传后保存 v2；同一会话拒绝第二次 start；仅现有公共协议调用。

## Task 3：开发页面、实际浏览器与交接

Files: 新增 `file-workflow/dev/TtsTaskDemo.tsx` / `tts-demo-transport.ts`、`scripts/dev-env/workbench-tts-probe.mjs`；修改 `AppEntry.tsx`；新增交接及索引。

Interfaces: `?dev-scenario=tts-task`；synthetic PCM WAV，开发 transport 只返回经 A parser 的合成资源；浏览器驱动真实页面。

- [x] RED/GREEN：正常音频试听/下载、未知创建原键重放、取消 pending、过期失败；小窗口/键盘与卸载清理；生产入口无 fake。
- [x] Web format/lint/typecheck/test/build、脚本显式 lint/format、既有 ASR 浏览器回归、变异验证、一次独立只读审查；重要问题一次 RED→GREEN 修复。
- [x] 交接明确基线、版本、证据范围与阻塞；累计补丁 apply-check/apply/逐文件字节、真实 index 不变。完整 goal 保持 active。

独立审查 P2：固定首次 Asset 的身份，阻止 staging→ready 刷新改变 workspace/purpose/media_type/bytes/sha256；10 项负例 RED→GREEN、2 项正例通过。59 项相关 / 278 项完整 Web 测试通过。延期 minor：TTS 初始提示沿用 ASR 文案。
