---
title: "团队 C C02 文本配音交接"
status: active
date: 2026-10-24
---

# 团队 C C02 文本配音交接

[本单元计划](2026-10-24-team-c-c02-tts-plan.md)推进 C02 的第二条基础闭环：文本上传、固定声音/参数版本的 TTS Job、事件读取、音频试听与下载。生产入口仍关闭；本单元不是完整 C02 或真实 TTS 资格声明，[完整 goal](2026-10-24-team-c-goal.md)保持 active。

## 基线与交付

`base_sha` / `head_sha`：`d7463fb3e6421d7fe57efdf99566f241a163c7bb`。核对上一包 97 文件全部摘要后，fast-forward 接入不重叠的 A02 资产持久化变更；C 改动仍未提交或推送。speech/workbench OpenAPI `info.version=0.1.0.dev0`，bindings/fixture `version=0.1.0`，本轮不修改契约、生成或共享依赖。

- `SpeechFileClient.uploadText` 使用真实 UTF-8 multipart 文件和 `purpose=text_input`。`FileTaskSession.startText(text, selection)` 提交前经 A parser 校验；拒绝会被 UTF-8 编码改写的文本。输入错误直接提示，不伪称操作结果未知。
- journal v2 只存 text asset/workspace、模型、voice/profile ID+revision、原创建/取消键、pending/operation 和水位。命令固定 WAV 输出；不存文本、音频、文件名或完整响应。v1 ASR 记录仍按原命令恢复；CAS、不可变意图、提交前写入屏障和取消恢复规则沿用。
- 复用同一 Job/SSE/取消控制，不复制 wire DTO/validator；TTS 响应须匹配 kind/workspace/已知 Job 身份。刷新只读；显式重放保持原 asset、原选择和原键，不使用修改后的表单。
- `?dev-scenario=tts-task` 仅开发模式动态加载。合成 transport 不回退真实 fetch；页面明确显示测试声音/参数与合成计数。支持未知创建、未知取消、音频过期、合成提示音试听/下载。替换或卸载停止播放器并撤销 Blob URL；播放结束更新提示。

## 验证证据

核验时间：2026-10-24（Asia/Shanghai）。Node `26.11.1`、pnpm `12.14.0`、Chromium `156.0.8078.4`；未升级工具链，Node 仍未达到 `.nvmrc` 精确版本。

- TDD：TTS 行为先 11 项失败；输入错误未知状态、UTF-8 改写和播放结束提示均另有 RED→GREEN。取消素材身份漂移的 10 项负例先失败，2 项 staging→ready 正例保持通过；最终 TTS/共用素材边界 27 项、原 journal/session 32 项，共 59 项相关测试通过。完整 Web 278 项（contracts 105、Admin 19、Workbench 154）通过。
- `pnpm format:check`、`pnpm lint`、`pnpm typecheck`、`pnpm build` 通过；新增/修改探针另做显式 Prettier/ESLint。移除 kind 核对的变异被“ASR 不得确认 TTS”测试检出，源码字节已恢复，随后全套 Web 通过。
- 实际 Chromium UI 4 场景：键盘 Enter 提交、原生 WAV 解码/播放、实际文件下载、未知请求重放与取消 pending、过期失败、卸载 URL 撤销、390px 无溢出。生产 preview 打开相同参数仍为正常外壳，出站请求/pageerror 为 0。
- 原 ASR 浏览器演练 9 场景修复后重跑通过。
- 真实 loopback HTTP + Chromium + sessionStorage：TTS 3 次刷新，创建/取消各 2 次 POST 沿用原键，仅 1 个创建意图；asset/workspace/voice/profile/output 固定，`9007199254740993` 水位精确恢复。记录 360 字符，无正文；出站、handler/page 错误为 0，服务已停止。ASR 模式同样通过。

```bash
pnpm --filter @speechnexus/workbench test src/file-workflow/tts-task.test.ts src/file-workflow/task-journal.test.ts src/file-workflow/file-task-persistence.test.ts src/file-workflow/file-task-session.test.ts
node scripts/dev-env/workbench-tts-probe.mjs --output .runtime/team-c/c02-tts-browser.json --screenshot .runtime/team-c/c02-tts-browser.png
node scripts/dev-env/workbench-recovery-probe.mjs --kind tts --output .runtime/team-c/c02-tts-reload-browser.json
```

证据在 `.runtime/team-c/c02-tts-*.log` / JSON 与 `c02-tts-progress.md`。Python/契约内容未修改；此前 Python/隔离 PG 证据只保留原基线和范围，不扩展为本轮 A02 新实现的运行验收。

## 执行决定与待接入项

1. 沿用既有批准的 C02 设计与本会话实施方式；共享 helper/reviewer 模板仍缺失，采用手工 ledger/package 和一次独立只读审查。代价是需逐文件核对未提交交付，不清理原有改动。
2. 固定 voice/profile revision 与 WAV 输出，文本由公开 text asset 引用。代价是部署必须提供实际可用选择；本轮不实现语音资格或格式转换。v1/v2 记录分别重建，不迁移或覆盖旧意图。
3. 只持久化已上传素材的 Job 阶段元数据，原文只在上传前驻留内存。代价是上传未知跨刷新仍待完成，生产宿主仍须关闭流程；损坏或不可用的 journal 不能被宿主忽略后另起意图。
4. 早期探针/测试使用了带音频字段的 text Asset、被读取过的 multipart Request，以及不符合 A wire 的 revision 上界假设。按权威契约修正测试材料，不放宽生产校验。代价是这些失败不计通过，仍不代表真实服务验证。
5. 一次独立审查发现 P2：校验中的 Asset 可在刷新后改变 workspace。首次响应即独立固定 id/workspace/purpose/media_type/bytes/sha256，刷新和创建前均核对，身份变化保留原基线且不 POST；允许状态合法转 ready。10 项 TTS/ASR 负例 RED→GREEN，一次集中修复后全套通过。代价是服务端须保持已受理素材的内容身份稳定；不以新身份替换旧素材。
6. 图谱未索引，主代理按源码核对；独立审查未复核旧 90 文件摘要/累计包，也未重跑浏览器/变异。主代理分别验证逐文件摘要、干净基线 apply/字节/index 和真实浏览器探针。代价是这些检查的直接证据来自主代理，且不扩展到真实 TTS/PG/GPU/设备或生产资格。

当前工作台 response/revision/export 生成入口、生产同源 transport/HTTP/对象区、上传未知恢复、校对保存与导出、真实语音/设备/GPU 联调仍未齐备。音频下载沿用 16 MiB 本地策略，不宣称支持长篇音频的流式 Range 播放。合成 UI 的状态只驻留内存；跨刷新证据由真实会话/journal 的独立 HTTP 探针提供。

累计补丁与摘要由 `.runtime/team-c/team-c-current.patch` / `team-c-current.manifest.json` 保存，累计 104 文件；在干净基线 apply-check、实际 apply 和逐文件字节匹配，真实 Git index 不变。前一 97 文件包另存 `team-c-c02-recovery-66bbc15.*`。

审查延期 minor：TTS 初始空闲状态沿用“选择音频开始转写。”，后续由宿主提供配音文案；不影响协议，未混入本轮重要问题修复。最终审查与包核验记录见本单元 ledger。
