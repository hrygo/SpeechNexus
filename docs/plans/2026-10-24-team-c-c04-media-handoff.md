---
title: "SpeechNexus C04 合成播放单元交接"
status: in-progress
version: "0.1.0"
date: 2026-10-24
---

# SpeechNexus C04 合成播放单元交接

后续采集实现及当前基线见 [C04 浏览器采集交接](2026-10-24-team-c-c04-capture-handoff.md)。以下保留原播放单元的时点证据；后续新增采集不代表公开 Realtime 或真实设备资格已通过。

## 身份与验收边界

团队 C / C04；持续范围见[团队 C 目标](2026-10-24-team-c-goal.md)。本轮交付独立播放单元及合成浏览器探针，C04、C08 和 Gate 均保持待完整验收。

工作基线为 `8333e4ba7c722a36cc70545fd5a0ed2beee38eea`，分支 `codex/team-c-c00-versions`。改动尚未提交，没有承载本单元的 `head_sha`，未 push 或更新远端 Issue。联合联调仍须确认共同 integration SHA。

本轮没有新增依赖或修改共享锁文件、机器契约、公开 DTO、生成客户端。内部 PCM/MessagePort 类型只描述本地播放，不替代公开 Realtime 协议。下一步消费 A00 的确定生成产物后，再映射公开 epoch、credit 和 gap 语义。

## 实现与文件

| 文件 | 行为 |
| --- | --- |
| `web/workbench/src/media/playback-buffer.ts` | 固定环形缓冲、连续 sample cursor；容量不足整块拒绝，欠载补零但不推进源 PCM 水位；停止清除缓冲 |
| `web/workbench/src/media/playback-channel.ts` | 复制并 transfer PCM；同时限制未消费样本和在途消息；按累计消费水位恢复本地额度；忽略旧 token，处理非法消费回报和处理器失败 |
| `web/workbench/src/media/playback.worklet.ts` | 实际 AudioWorklet 处理器；单声道输出，约 100ms 或队列清空时报告消费；非法当前消息清空并停止，旧 generation 消息忽略 |
| `web/workbench/src/media/worklet-playback.ts` | 每个调用方持有的 context 复用模块、每个 generation 创建节点；新播放停止旧播放；异步加载期间的 abort 立即结束启动，旧启动不得迟到连接；失败和停止关闭 port、断开节点、移除监听 |
| `web/workbench/src/media/*.test.ts` | 环形回绕、样本顺序、背压、非法消息、累计 ACK、停止、异步竞争与取消回归 |
| `scripts/dev-env/workbench-media-probe.mjs` | Vite 生产打包播放模块及 worklet，以 loopback HTTP 和无设备权限的 headless Chromium 验证 OfflineAudioContext 输出；输出元数据，不写音频 |

本地内存策略：环形缓冲最多 `1_048_576` 个 Float32 样本（4 MiB），每块最多 `8192` 样本，在途消息最多 `64`。这是应用内部上限，不是公开 WS credit 或服务端容量保证。欠载时源样本 cursor 保持不变，不把静音误算为已消费 PCM。

调用方负责 AudioContext 生命周期、用户手势、样本解码和采样率转换；输入 PCM 时钟须等于 context.sampleRate。本单元不请求麦克风、不隐式重采样、不关闭调用方共享 context。停止播放只证明本地节点释放，不证明服务端执行或 GPU 已停止。

## 核验与结果

核验日期：2026-10-24（Asia/Shanghai）；macOS，实际 Node `v26.11.1`、pnpm `12.14.0`、TypeScript `6.0.3`、Vitest `5.0.5`、Vite `8.3.5`、Chromium `156.0.8078.4`。实际 Node 满足 `package.json` 的 `>=26 <27`，但与 `.nvmrc` 的 `26.12.1` 不完全一致；本轮未升级工具链。

| 命令 | 结果及边界 |
| --- | --- |
| `pnpm install --frozen-lockfile` | 成功，共享 package/catalog/lockfile 无改动 |
| `pnpm --filter @speechnexus/workbench exec vitest run src/media` | 42 passed；涵盖实际处理器模块的 12 项测试，使用最小渲染线程 host fake |
| `pnpm test` | Workbench 44 passed，Admin 3 passed |
| `pnpm format:check` | passed；探针另以 `pnpm exec prettier --check scripts/dev-env/workbench-media-probe.mjs` 检查通过 |
| `pnpm lint` | passed；探针另随 `pnpm exec eslint web scripts/dev-env/workbench-media-probe.mjs --max-warnings 0` 检查通过 |
| `pnpm typecheck` | shared / Admin / Workbench passed |
| `pnpm build` | Admin / Workbench passed；应用尚未消费本模块，此命令本身不证明 worklet 已进入应用 bundle |
| `node scripts/dev-env/workbench-media-probe.mjs --output .runtime/team-c/c04-media-probe.json` | 独立构建生产模块和 worklet 后真实 Chromium 执行通过；24kHz 下 256 个合成样本从 offset 0 精确输出，背压生效、消费恢复额度、stop/abort/replacement 后静音、旧 generation 丢弃；无出站请求或 pageerror |

先观察“addModule 未完成时 abort 仍挂起”的回归失败，再实现取消等待并通过；同时验证取消不清除仍在加载的模块缓存、真正加载失败可由新 generation 重试。独立只读审查未发现当时单元的必须修复问题，并建议持久化处理器拒绝路径测试；已补充当前非法消息清空队列和停止的回归测试。

探针只使用合成 PCM 和临时生产 bundle，运行后关闭浏览器/HTTP 并清理其临时目录。统计结果保存在仓库忽略的 `.runtime/team-c/c04-media-probe.json`；没有录音、真实业务媒体或外部服务操作。

## 交接与剩余工作

当前累计改动（C00 + C04 + 文档）补丁位于 `.runtime/team-c/team-c-current.patch`，校验值在交接回复中提供。补丁包含新增文件，并在基线副本中进行 `git apply --check`、实际应用和逐文件字节比对。原 C00 补丁保留其原时点内容。

- C04 未验证：真实设备/权限/自动播放、采集和重采样、后台节流、持续实时压力、Safari/跨浏览器、fake/public WS 连接和恢复、服务端 credit/gap 映射、服务端停止证据；尚未接 UI 消费者。
- A00 依赖：公开生成类型、客户端和运行时 validator 的确定版本；C 不手写独立公开语义副本。A/B 联调资格和 GPU 结果由对应 owner 提供。
- C 可继续独立推进 C01/C02 的开发 fake 业务切片；C00 的生产持久化、删除恢复及真实文件闭环仍按[C00 交接](2026-10-24-team-c-c00-version-handoff.md)待集成。

本轮 Web 改动未触及 C00 Python 源码；复用其原时点 270 项单元/契约测试证据，不宣称本轮重新执行 Python 或 Linux/PG/MinIO 验证。
