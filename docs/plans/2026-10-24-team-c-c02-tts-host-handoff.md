# C02 正式文本配音入口交接

核验日期：2026-10-24（Asia/Shanghai）。本单元属于完整 C00–C08 active goal，不代表完整 C02 或专业资格完成。

## 基线与所有权

当前 base/head：`dc14cf8fb0a7250842a8a7697c6884d6d21b2004`；契约包 `0.1.0`，消费 A 的生成类型、runtime validator 和上传 helper。私有恢复记录沿用 v2/v4/v6，WAV 选项不变；仅增加私有视图 `inputFixed`，未改机器协议或生成产物。

启动时累计 152 文件包 SHA-256：`b78a55a5cb18644ab7a5df396c228ac7d60da6c85e013f925c59e9a86969ac26`。保留旧包和逐文件 hash；新单元只修改 Workbench 正式 App、任务表单/结果/生命周期、相关测试、探针及中英 README/交接索引。Backend/Admin/共享协议与既有交付保持原字节。未 commit、push 或发送外部消息。

已 fetch 到上游 `3e485ee` / `6e8fcd8`，README 存在并行改动；本单元保持上述基线，不自动合并累计未提交文件。A/B 新提交的运行资格不能计入本单元结果。

## 实现与限制

正式 App 可切换“文件转写”和“文本配音”；只挂载一个宿主，切换时停止事件/音频并释放会话。未提交输入会清空，服务器任务不因此取消；返回时从独立恢复槽显式读取。ASR key 为 `speechnexus.workbench.asr-task.v1`，TTS key 为 `speechnexus.workbench.tts-task.v1`。存储失效、损坏、异类和字符串版本 fail closed，保持原记录，无内存降级或替代提交。

TTS 固定部署模型、声音 ID/revision 和参数 ID/revision；无虚构默认值。生成 validator 与 UTF-8 检查成功后才固定输入，校验失败仍可修改。接受素材后清空页面正文，本地只保存键/指纹/引用/版本。刷新无自动请求；hint 只读原 Asset，ready 后显式创建；无 hint 需原文本指纹匹配再按原键恢复；创建未知保持原键和固定选项。取消和新任务仍沿用共同确认/owner/清理政策。

结果仅消费当前 Job `tts` 的已提交 audio_artifact_id。原生播放器和下载仅接受 audio/WAV 元数据，沿用有界 16 MiB 下载。试听、停止、隐藏、pagehide、卸载和新一次加载通过 generation 防止迟到 URL/播放/DOM 下载；预览 URL 显式释放，下载临时 URL 在点击后释放。服务端失败仅呈现稳定状态，不展示原始错误正文。

Model/Voice/Profile 的发现 runtime validator 尚未发布；暂由用户向管理员核对并填写真实部署引用。这不是发现/能力列表或质量资格。校对、业务版本/字幕导出、声音工作室、会议/实时、助手/LLM、有声书及真实后端/模型联调继续按 C00–C08 推进。

## 实测证据

- Workbench `254/254`、Admin `79/79`、contracts package `121/121`，共 `454` 项通过；全 Web format/lint/typecheck/test/build 通过。Workbench bundle `463.66 kB` / gzip `133.23 kB`，无新增依赖。
- TTS 宿主 `19` 测试：校验后锁定、无正文存储、严格独立槽、原键/版本恢复、hint 与原文本指纹，以及停止/隐藏/pagehide/卸载期间迟到的试听和下载。
- mutation 实验移除 generation guard，`4` 条迟到用例失败；逐字节恢复原文件后 `19/19` 通过。
- 正式生产包 Chromium `156.0.8078.4` + loopback 合成 HTTP：42 字节 UTF-8、text/plain 文件与固定版本；原 Asset hint/无 hint 原文本恢复、未知创建原键重放、刷新不请求、SSE `9007199254740993/4` 水位、原生解码 1 秒/48044 字节 WAV、下载逐字节一致、停止丢弃迟到内容、切换撤销 URL、键盘焦点与 360px 无横溢。
- 新探针全部 `3` 个 context 均监测出站和 pageerror；`3` 次上传、`3` 次创建请求（含原键重放），handler error / 非同源请求 / pageerror 均为 `0`。修正了探针自身 about:blank 存储注入错误后重跑，无例外豁免。
- 正式 ASR 探针继续通过：6 上传/5 创建/2 取消；旧文件开发 UI `9` 场景、旧配音开发 UI `4` 场景和 Asset 恢复 `10` 场景回归通过。旧 ASR 探针 blocked context 未监测的既有 P3 仍按前单元记录；不把其局部监测扩大为所有 context。
- 截图已查看，双栏/窄屏/播放器无溢出；正式 bundle 排除演练 transport。没有加载模型、访问真实音频/书稿、请求设备权限或修改运行服务。

可复现命令（仓库根目录）：

```bash
pnpm format:check
pnpm lint
pnpm typecheck
pnpm test
pnpm build
node scripts/dev-env/workbench-tts-host-probe.mjs --output .runtime/team-c/c02-tts-host-browser.json --screenshot .runtime/team-c/c02-tts-host-desktop.png
node scripts/dev-env/workbench-file-host-probe.mjs --output .runtime/team-c/c02-tts-host-asr-regression.json
node scripts/dev-env/workbench-file-demo-probe.mjs --output .runtime/team-c/c02-tts-host-file-regression.json
node scripts/dev-env/workbench-tts-probe.mjs --output .runtime/team-c/c02-tts-host-tts-regression.json
node scripts/dev-env/workbench-asset-reconcile-probe.mjs --output .runtime/team-c/c02-tts-host-asset-regression.json
```

原始日志、截图、终审和累计包保存在忽略目录 `.runtime/team-c/`。本轮不改 Python、数据库、执行网关或契约，相关集成资格未重跑；沿用前单元未变的 canonical/生成漂移证据，并在最终累计包复核源字节。图谱无本项目索引且 analysis 工具不提供重建，按已知路径读取源码，不宣称全图覆盖。

## 终审与交付状态

独立只读终审结论为 Critical 0 / Important 0 / Minor 0；审查者另行复跑 40/40 相关测试、typecheck、探针语法与 diff 检查，通过。没有需要进入一次 TDD 修复的已确认问题，未复审。

累计交付 159 文件：旧 152 中 142 保持原字节、本单元修改 10、新增 7。使用临时 index 生成 patch，在干净 dc14cf8 archive 上 apply 并逐字节比对，核对 229 个本地文档链接；真实 index 保持空且字节不变，canonical/生成/lock 文件未改。当前包路径 `.runtime/team-c/team-c-current.patch` 和 manifest，旧 152 文件包另存 `team-c-c02-host-dc14cf8.patch` / manifest，不覆盖历史证据。

本单元没有新增暂缓 Minor。此前独立审查的 generic journal 字符串版本 P3、旧 ASR blocked-context 监测 P3 仍在原交接记录；正式宿主的严格版本拒绝和新 TTS 全 context 监测已经覆盖各自入口，不据此宣称旧问题全局消失。

## 执行裁决

1. 沿用授权工作树和手工 ledger/package，因为 sdd-workspace/review-package 模板缺失；成本是人工归属和完整性核验。
2. 上游与本地 README 并行修改时保留当前基线；成本是暂未吸收 A/B 两次新提交。
3. 发现 validator 缺失时手填真实引用、不维护 DTO 副本；成本是发现/资格继续依赖 A，用户需知道引用。

4. 不授予真实后端/模型/声音/profile、质量、设备、负载或部署出站资格；成本是这些场景仍需真实验收。
5. 不扩大本次终审到旧 152 文件、A/B 新提交或完整 C00–C08；成本是整体仍需各单元与整体验收，goal 保持 active。
6. 原生播放器/下载实测限于 Chromium 156；成本是其他浏览器/OS 尚需独立验证。
7. 未来发现 validator 未交付，不据此推断可用性；成本是当前无动态发现、用户需知道真实引用。
