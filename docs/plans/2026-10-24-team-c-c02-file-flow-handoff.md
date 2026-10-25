---
title: "SpeechNexus C02 文件任务恢复与 A00 消费交接"
status: in-progress
version: "0.1.0"
date: 2026-10-24
---

# SpeechNexus C02 文件任务恢复与 A00 消费交接

## 基线与范围

团队 C / C00–C02 的首个 A00 客户端消费单元，持续目标见[团队 C 目标](2026-10-24-team-c-goal.md)。本轮交付 ASR 文件任务的开发演练和恢复控制，不关闭 C00/C01/C02 或任何 Gate。

核验到 A00 首包已随 PR #46 合入主线，随后 PR #47 优化生成兼容检查及 CI。先检查来件与本地改动无路径冲突，再 fast-forward 到 `b3e0ed340763b827333747d464b68fd7f4981116`。当前分支仍为 `codex/team-c-c00-versions`，C 的改动未提交，没有承载本单元的 `head_sha`；没有 push、远端 Issue 更新或外部消息。本 SHA 是本地消费基线，联合联调仍以 A 指定的共同 SHA 为准。

消费 `packages/contracts` 的 TypeScript 包 `0.1.0`、生成 speech 类型和 file-workflow validator；源协议参照该 SHA 的 `contracts/bindings.json` 与 `contracts/speech-openapi.yaml`。本轮不修改生成产物或机器契约，不解析内部执行协议，不访问引擎或数据库。

## 已交付

| 范围 | 文件与行为 |
| --- | --- |
| 公开边界消费 | `web/workbench/src/file-workflow/speech-file-client.ts`：实际调用 A 的 `createSpeechClient` 和 `parse`；上传真正 multipart 文件、读取 Asset/Job/Operation、创建、取消、SSE 和原始 Artifact 下载；不信任静态类型，额外核对请求与返回资源 ID |
| 恢复控制 | `file-task-session.ts`：一个文件意图固定上传/创建/取消键；创建载荷经 validator 后固定，重放不依赖刷新后的素材状态；未知写入不自动重提；恢复错误保留原执行身份和回执字段，通过 Operation 查询找回 Job；取消响应与清理证据分开展示 |
| 事件恢复 | `sse.ts` 和 session：有界帧、跨 chunk CRLF、多行 data、UTF-8 解码、取消 pending read；validate JobEvent 并核对 envelope；用 decimal/BigInt 水位去重，事件只触发权威 Job 重读；410 历史过期后清水位并重读当前状态；旧 watcher 不覆盖新操作 |
| 下载 | 先读取 Artifact 元数据，非 ready 不下载；16 MiB 本地上限；流式计数拒绝超过声明长度和截断，失败/取消时释放 reader。保留原始字节，不把本路径称为校对导出或独立保存 |
| 开发界面 | `file-workflow/dev/FileTaskDemo.tsx`、`demo-transport.ts`、`file-task-demo.css`：真实点击合成 WAV 上传→任务→事件→下载，9 个确定故障场景；共享 Token 和部署边界常驻，能力标为 contract，演练计数明确区分真实遥测；StrictMode 下真实卸载才销毁 session |
| 模式隔离 | `AppEntry.tsx`、`main.tsx`：只有 `import.meta.env.DEV` 且查询参数 `dev-scenario=file-task` 时动态导入演练；生产入口仍为现有契约外壳 |
| 可复现验证 | 三份新增测试文件、`scripts/dev-env/workbench-file-demo-probe.mjs`；浏览器探针拒绝真实 `/v1/` 与出站请求，不申请设备权限 |

实现选择：C 通过 A 包唯一公共入口的源码路径导入，复用同一客户端、类型与 validator。这样本轮无需写入 A 协调的共享 manifest/lockfile；代价是当前消费要求完整 monorepo 源码在场，尚不能作为独立发布包使用。正式 workspace 依赖与锁文件集成仍由 A 协调，不保留第二份 DTO 或 validator。

开发演练入口（从仓库根目录执行）：

```sh
pnpm --filter @speechnexus/workbench dev
```

打开 `http://127.0.0.1:5174/?dev-scenario=file-task`。只上传页面生成的合成静音文件，不读取真实录音；每次切换场景重新建立内存会话。场景涵盖正常、创建响应丢失、取消响应丢失、幂等冲突、历史过期、制品过期、失败/隔离、持久 SSE 及原执行查询。fake 从不调用底层 fetch，也不持久保存文件或业务数据。

## 验证证据

核验时间：2026-10-24（Asia/Shanghai）。实际 Node `v26.11.1`、pnpm `12.14.0`、TypeScript `6.0.3`、Vitest `5.0.5`、Vite `8.3.5`、Chromium `156.0.8078.4`、Python `3.14.9`。Node 满足 package engines，但未达到 `.nvmrc` 的精确 `26.12.1`；本轮不升级工具链。

| 检查 | 结果 |
| --- | --- |
| `pnpm install --frozen-lockfile`、`uv sync --locked --all-packages --all-groups` | 新基线锁定安装成功；未改 manifest/catalog/lockfile |
| `pnpm --filter @speechnexus/workbench exec vitest run src/file-workflow` | 17 passed；multipart 测试用 Node 原生 File/FormData/Request，避免与 jsdom 跨 realm 的假失败 |
| `pnpm test` | Workbench 61、Admin 3、A00 包 51 项全部通过；没有跳过 |
| `pnpm format:check`、`pnpm lint`、`pnpm typecheck`、`pnpm build` | 通过；探针脚本另做显式 Prettier / ESLint 检查 |
| `uv run --locked pytest tests/unit tests/contract -q` | 新基线 343 passed；不包括服务集成 |
| `make contracts-drift` | 生成类型、schema 和 source inventory 无漂移；不等于业务语义已全部冻结 |
| `node scripts/dev-env/workbench-file-demo-probe.mjs --output .runtime/team-c/c02-file-demo.json --screenshot .runtime/team-c/c02-file-demo.png` | 真实 Chromium 9 场景通过：原键重放只有一个合成执行、取消保留 pending、410 重读、精确 `9007199254740993` 水位、持久事件连接时仍可取消、原 Operation 查询、制品过期、合成原始内容下载；390px 无横向溢出；无 pageerror/真实服务/出站请求 |
| 生产隔离 | Vite 生产资产不包含场景/fixture 标记；浏览器在生产 preview 打开相同查询参数，仍显示正常外壳且没有故障选择器 |

运行浏览器探针前需已有 `pnpm build` 产物及已安装的项目 Chromium。探针自行建立并关闭 loopback Vite dev/preview 与浏览器，报告和截图保存在忽略目录 `.runtime/team-c/`，不写入语音服务。

独立只读审查发现 3 项问题并完成集中修复：素材刷新使原创建重放消失、持久 SSE 锁住操作、恢复身份被丢弃。先分别观察两个单元回归失败和一个真实浏览器“取消按钮禁用”失败，再修复并通过全套检查。审查没有扩大为真实服务验收。

## 交接与剩余工作

累计本地补丁为 `.runtime/team-c/team-c-current.patch`，本轮校验值在交接回复中提供；包含之前 C00/C04 与本轮全部新增文件。在当前基线副本上执行 apply check、实际应用和逐文件字节比对。之前的 C00/C04 交接保留其当时基线与验证结果；本文件说明 A00 依赖已经变化。

- C00/C02：仍需 workbench 的生成边界、原始转写/receipt 导入、项目/稿件 API、校对历史/冲突及文本/字幕导出协议；没有用本地可编辑文本假装已保存。A00 当前运行时入口只覆盖已公布文件工作流。
- C01：Admin 能力/任务视图和 admin 生成绑定尚待接入，当前演练不能证明管理面已完成。
- C02：目前是 ASR 开发切片；真实上传校验、TTS、服务端事件持久化、生产幂等/恢复及完整校对下载闭环未验证。
- C04/C08：真实设备、采集/重采样、长期实时压力、跨浏览器、公开 WS credit/gap 及 GPU 联调仍按[C04 交接](2026-10-24-team-c-c04-media-handoff.md)待验；没有把 Chromium fake 通过当作生产资格。
- A/C 接力：A 协调正式 workspace 依赖/锁文件和剩余 admin/workbench 生成入口；C 消费确定版本后接生产适配和真实页面。A02/A04/B03 未提供本机可验证闭环前，继续推进独立媒体与工作台业务单元。

图谱未收录本仓库，当前 analysis 工具面不能重建索引；源码结论来自实际文件读取和验证，不声称图谱覆盖。没有 commit/push、外部服务配置、Linux 后端/镜像、PG/MinIO/MySQL 或真实模型操作。
