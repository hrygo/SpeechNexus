# C02 上传阶段跨刷新恢复实施计划

> 执行：`executing-plans` 在本会话实施，TDD、一次独立只读审查，不提交/推送。

**Goal:** ASR 音频或 TTS 文本上传结果未知时，跨刷新保留原键和输入身份；重新提供同一输入后显式原键恢复，已知素材只读恢复，最终交接到原 Job 意图。

**Architecture:** 同一 `SessionTaskJournal` 原子推进版本 3/4 上传记录到既有 v1/v2 Job 记录，避免双 journal 失配。上传记录只保存请求键、模型/选择、分块输入指纹与已确认素材身份，不保存正文、文件名或音频。固定输入指纹不是 wire SHA256，也不是服务端接受/停止证据。

**Tech Stack:** TypeScript / A 公开 client/parser / WebCrypto / Vitest / Chromium。

**Spec:** [三团队 C02](2026-10-23-three-team-delivery-plan.md#43-c-团队应用与体验)刷新恢复/重试不重复发布；[上一单元](2026-10-24-team-c-c02-tts-handoff.md)的上传阶段缺口；`contracts/speech-openapi.yaml` 的公开 Asset/Job 协议。

## 边界与审查重点

- 不改契约、生成、共享锁、组合根或服务端状态；不另造按键查询的 HTTP 接口。只重放原公开上传键，不宣称真实底座可用。
- 单 key CAS，原 upload/create/cancel 键、模型与 voice/profile revision 不可变；已确认素材 id/workspace/purpose/media_type/bytes/sha256 不漂移。
- v3/v4 上传意图提交前必须持久化；确认素材后先持久化其身份，Job POST 前原子写入 v1/v2 pending-create。损坏、存储失败或过期控制器不得开启替代意图。
- 分块 1 MiB，WebCrypto SHA256 链，域标记与 file.name/type/size 进入指纹；不存文件名、内容、完整 Asset。相同内容不同文件身份明确不算原输入。ASR 不整段 arrayBuffer；abort/dispose 后不得写记录或 POST。
- 刷新只读；上传未知且原 File/文本已不在内存时 `requiresInput=true`，不能自动 replay；匹配输入后才显式提交。已确认素材 GET 后可由用户显式继续原任务。

## Task 1：输入指纹与版本化 journal

Files: 新增 `input-fingerprint.ts` / `upload-recovery.test.ts`；修改 `task-journal.ts`。

Interfaces: `inputFingerprint(file, signal?)`、`textUploadFile(text)`；`UploadRecord` v3 ASR/v4 TTS、`RecoveryRecord=TaskRecord|UploadRecord`；`FileTaskJournal.load/save` 处理联合。`uploadJobRecord(record)` 只在素材已确认时生成原 v1/v2 命令身份。

- [x] RED：相同输入稳定、内容/文件名/type/size 改变失配，分块有界与取消；v3/v4 白名单/损坏/旧写；固定键/选择/素材；只允许合法阶段推进。
- [x] GREEN：实现指纹与单记录迁移，v1/v2 正常恢复不变；不写第二套 wire validator。

## Task 2：会话接入上传恢复

Files: 修改 `file-task-session.ts` / `speech-file-client.ts`；新增 `upload-recovery.test.ts`，调整旧测试对“确认后存储失败”的故障注入为逻辑阶段。

Interfaces: 原构造器不变；新增 `resumeFile(file)` / `resumeText(text)`；`TaskView.requiresInput`。上传键从恢复记录取得；start 不覆盖已存在意图，restore 不 POST。

- [x] RED：上传响应丢失→销毁→恢复不 POST→错误输入拒绝→原输入原键上传/创建；已确认 staging 素材跨刷新→GET→显式继续；ASR/TTS 分别验证；写入前/素材确认后/Job 交接失败、身份漂移与 dispose 迟到。
- [x] GREEN：先保存上传 intent，素材确认后保存身份，原子交接 Job；错误/原输入缺失不丢键或伪确认。

## Task 3：浏览器与交接

Files: 修改 `scripts/dev-env/workbench-recovery-probe.mjs`；新增本单元 handoff，更新索引/goal。

- [x] 真实 loopback HTTP 故意丢上传响应，ASR/TTS 跨刷新后拒绝错误输入，按原键恢复，创建不重复；元数据无内容，服务停止/0出站，明确合成证据。
- [x] Web format/lint/typecheck/test/build、脚本校验，原 ASR/TTS 浏览器回归；变异证明输入匹配屏障；一次只读审查，重要问题一次 RED→GREEN 修复。
- [x] 累计补丁在干净基线 apply-check/apply/逐文件核验，真实 Git index 不变。完整 C00–C08 goal 仍 active。
