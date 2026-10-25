---
title: "团队 C C02 上传阶段刷新恢复交接"
status: active
date: 2026-10-24
---

# 团队 C C02 上传阶段刷新恢复交接

[本单元计划](2026-10-24-team-c-c02-upload-recovery-plan.md)补充 ASR/TTS 上传结果未知、已确认素材校验中和 Job 提交之间的跨刷新恢复。生产宿主尚未启用；[完整 C00–C08 goal](2026-10-24-team-c-goal.md)仍 active。

## 基线与行为

`base_sha` / `head_sha`：`eb710ab61fb1e72099e915822b5e9ec4a6ab44aa`。上一 104 文件包全部摘要核对后，fast-forward 接入不重叠的 B 停止证据变更。C 改动仍未提交或推送。公开 speech/workbench OpenAPI `0.1.0.dev0`、bindings/fixture `0.1.0` 不变；未修改契约、生成、共享依赖或后端。

- 注入 journal 的会话在上传 POST 前保存 v3 ASR / v4 TTS 意图；只存原 upload/create/cancel 键、模型/固定声音参数、输入指纹和已确认素材身份。原文、音频、文件名不写入存储。
- 输入指纹使用带版本和文件 name/type/size 的 SHA256 链，每块 1 MiB；只用于重新选择输入时核对，不是 wire 内容 SHA 或声音特征。必须完整读取输入，但内存有界；文件改名也需重新提供原名。
- 上传未知后刷新只读，`requiresInput=true`，不能无输入重放。`resumeFile` / `resumeText` 先核对指纹，错误输入不 POST、不改记录；原输入沿用原上传键。已确认素材按原 ID GET，身份变化拒绝继续，ready 后由用户显式继续。
- 确认素材后先保存 id/workspace/media_type/bytes/sha256，再原子迁移为原 v1/v2 pending-create。一个存储 key / CAS 保护整个阶段；失败保留原意图，不创建替代键。旧 v1/v2 Job 恢复、取消和事件水位沿用。

## 验证

核验时间：2026-10-24（Asia/Shanghai）。当前 Node `26.11.1`、pnpm `12.14.0`、Chromium `156.0.8078.4`；Node 未达到 `.nvmrc` 精确版本，未升级工具链。

- 新模块缺失 RED 后，行为基线 11 失败 / 5 通过；审查修复后新增 25 项、原文件工作流 69 项，共 94 项通过。覆盖 ASR/TTS 原输入/原键、staging 刷新、各阶段存储失败、身份漂移、过期控制器和 dispose 后迟到写入。后加生命周期案例不宣称逐项观察过 RED。
- 反转输入匹配条件后，两种任务的原输入恢复回归均失败；精确恢复源码字节后验证。修复后完整 Web 303 项（contracts 105、Admin 19、Workbench 179）通过。format、lint、typecheck、build 及修改探针独立校验通过。
- 实际 Chromium + loopback HTTP + sessionStorage：ASR/TTS 各 4 次刷新；错误输入无请求，原输入 2 次上传 POST / 1 上传意图；创建 2 次 POST / 1 Job 意图；取消保留原键，精确恢复 `9007199254740993` 水位。无正文存储、出站和 page/handler 错误为 0，服务已停止。
- 原 ASR UI 9 场景、TTS UI 4 场景回归通过；TTS 包含原生 WAV 解码/播放结束、下载、键盘、390px、Blob URL 清理和生产构建排除。

```bash
pnpm --filter @speechnexus/workbench test src/file-workflow/upload-recovery.test.ts
node scripts/dev-env/workbench-recovery-probe.mjs --kind asr --upload-unknown --output .runtime/team-c/c02-upload-asr-browser.json
node scripts/dev-env/workbench-recovery-probe.mjs --kind tts --upload-unknown --output .runtime/team-c/c02-upload-tts-browser.json
```

探针均为合成内容和受控响应丢失，不证明真实语音服务、生产幂等、GPU、物理设备或 Linux 部署资格。生产入口、workbench response/revision/export 生成入口、校对保存/导出及真实服务接入仍待完成；不能把底座接受、取消或停止证据由前端推断出来。前一单元交接保留其原时点上传缺口描述，以本单元补充证据为准。

## 执行决定与交付

沿用批准的 C02 设计与本会话实施；缺失共享 helper/reviewer 模板以手工 ledger/package 和一次独立只读审查替代，代价是显式逐文件核对。保留当前分支及全部既有改动，不提交、推送、发送消息或清理。

一个 journal 的版本迁移保持原键和 CAS，代价是须同时验证旧记录及新转换；抽出 `upload-record.ts` 降低解码器负担，代价是 helper 与 journal 必须一起审阅。指纹全量读输入但有界内存，文件改名不能作为原输入恢复；上传未知需要用户重新提供原输入，并依赖服务端真正支持同键重放。

旧测试把“素材未 ready”理解为存储为空，并按第 2 次写入注入确认失败；改为保留 validation 记录、按 Job 确认逻辑阶段注入失败，继续断言原身份不变和无多余 POST，代价是不再依赖写入次数。lint 的测试 hook/JSON 类型问题已修正，没有改变生产行为。

图谱核验返回 `project not found or not indexed`，主代理按源码复核，不擅自创建索引；结构结论只覆盖已读源码。最终审查、延期 minor、累计补丁摘要与干净基线 apply/逐文件字节/index 结果记录于 `.runtime/team-c/c02-upload-progress.md`；累计交付仍由 `team-c-current.patch` / `team-c-current.manifest.json` 标识，旧 104 文件包另行保留。

一次独立只读审查发现 P2：已有上传记录让恢复路径跳过提交前 journal 检查，过期控制器或存储不可用时仍先重传。新增 ASR/TTS stale、getItem/setItem 失败共 6 项测试，先观察额外 POST 的失败，再修正为每次上传前保存同一意图；25 项定向测试通过，随后重跑完整 Web 与浏览器。未产生替代键或额外 Job，原记录不被覆盖。修复代价是每次原键上传重试多一次小型元数据写入，存储不可用时必须先恢复存储。

审查未独立核验真实后端/生产/设备/GPU/数据库/部署、范围外 96 文件与 A/B 变更、累计补丁应用或完整 Web/UI/变异；主代理核对旧摘要、干净基线应用/字节/index 和完整测试及浏览器证据。代价是旧逻辑沿用之前审查范围，广泛验证来自主代理，生产资格仍不关闭。双方均未取得图谱索引，结论限已读源码。

延期 minor：此前 TTS 初始闲置提示仍为“选择音频开始转写。”；由后续宿主接入时处理。本单元无新增 minor。
