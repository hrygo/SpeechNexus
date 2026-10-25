---
title: "SpeechNexus C04 浏览器采集单元交接"
status: in-progress
version: "0.1.0"
date: 2026-10-24
---

# SpeechNexus C04 浏览器采集单元交接

## 身份与边界

团队 C / C04 #39、C08 #43；[持续 Goal](2026-10-24-team-c-goal.md)保持 active。基线 `f210821d655bd7deb326825d750d83584c3ce159`，分支 `codex/team-c-c00-versions`。本轮新增本地采集模块、测试和独立探针，未 commit/push、发送外部消息、修改共享配置或访问物理麦克风。原有 51 个交付文件在本轮代码验证后逐文件核对摘要均未改变，随后只补充相关文档链接。

依据[实施单元](2026-10-24-team-c-c04-capture-plan.md)及 C04/C08 的资源生命周期要求。本地 Float32 PCM、token 和累计 ACK 仅为 MessagePort 内部传输，不是公开 Realtime DTO、epoch、credit 或 gap 协议。生成包仍未提供 realtime 运行时 validator，本单元未建立字段/默认值副本，未接 WS、真实服务或应用 UI。

本轮也核对了[删除恢复规范](../architecture/data-deletion.md#备份恢复)：独立账本保管、恢复水位及开放读取前重放要求仍未落地，外部消费契约需要 A07/C08 对齐。未用本地采集交付替代该要求。

## 实现

| 文件 | 职责 |
| --- | --- |
| `web/workbench/src/media/capture-buffer.ts` | 固定分帧缓冲、连续本地样本水位、累计 ACK、未确认帧上限；额度耗尽显式停止，不静默丢样 |
| `web/workbench/src/media/capture.worklet.ts` | 实际 AudioWorklet 采集处理器、transfer PCM、处理 ACK/停止及旧 token，输出保持静音 |
| `web/workbench/src/media/microphone-capture.ts` | 独立持有 stream/context，启动期限、串行消费、取消/页面/设备/处理器生命周期、清理结果与稳定本地错误 |
| 三个相邻 `.test.ts` | 41 项有界生产者、实际处理器 host fake 和采集生命周期测试 |
| `scripts/dev-env/workbench-capture-probe.mjs` | 临时生产 bundle、loopback Chromium、合成输入设备、真实音频图及实际 iframe 导航；只输出状态和计数 |

调用方显式传入 `sampleRate`、`frameSamples`、`maxPendingFrames` 和 `onFrame(frame, signal)`。`frameSamples` 范围 1–8192、未确认帧范围 1–64；它们是本地内存策略，不能当作服务端额度。模块最多持有固定一帧生产缓冲及有界未确认帧；调用方若另存 PCM，须自行管理其内容生命周期。消费 callback 串行完成后才 ACK，callback 可以 transfer buffer，水位使用 transfer 前固定的帧末位置。

采集独占 AudioContext，显式请求采样率并核对实际值。浏览器音频图负责转换，模块不自行线性降采样。节点以 explicit/speakers 单声道下混；worklet 输出零值，避免监听麦克风和回授。运行端仍须用生成协议校验其选定的公开 PCM 描述，不能因本地时钟可创建就宣称服务端支持。

从用户手势调用入口，默认启动期限 30 秒，可传 AbortSignal。用户取消、页面隐藏/pagehide、track ended/mute、context 中断、processor 错误、消费者失败或本地 overrun 均终止该次采集；中断后须新建会话/token，不能默默跳过音频继续。停止不等待消费 callback；立刻取消其 signal、停止 tracks、断开图和 port，并关闭 owned context。`closed` 等待 context 清理结果，清理失败会拒绝并标记 failed，不伪报完全释放。

浏览器原生权限请求本身不能被本模块撤销；应用等待可以取消或超时，若之后才授权，原生 fulfillment 当场接管并停止所有 tracks，不创建迟到节点。该单元停止只证明浏览器侧处理，不证明服务端执行/GPU 停止。

音频图与时钟语义参考 [W3C Web Audio](https://www.w3.org/TR/webaudio/)，track 生命周期参考 [W3C Media Capture and Streams](https://www.w3.org/TR/mediacapture-streams/#dom-mediastreamtrack-stop)（核验日期 2026-10-24）。浏览器实际支持以本轮探针为准。

## 验证

核验时间：2026-10-24（Asia/Shanghai）；macOS、Node `v26.11.1`、pnpm `12.14.0`、Chromium `156.0.8078.4`。Node 满足 package 的 `>=26 <27`，与 `.nvmrc` 的 `26.12.1` 不完全一致，未升级工具链。

```bash
pnpm --filter @speechnexus/workbench exec vitest run \
  src/media/capture-buffer.test.ts src/media/capture.worklet.test.ts \
  src/media/microphone-capture.test.ts
pnpm format:check
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm exec prettier --check scripts/dev-env/workbench-capture-probe.mjs
pnpm exec eslint scripts/dev-env/workbench-capture-probe.mjs --max-warnings 0
node scripts/dev-env/workbench-capture-probe.mjs \
  --output .runtime/team-c/c04-capture-probe.json
```

| 检查 | 结果与证据范围 |
| --- | --- |
| 聚焦 Vitest | 41 passed：生产者 14、处理器 host fake 5、生命周期 22 |
| 全 Web 测试 | 156 passed：workbench 102、admin 3、contracts 51 |
| 静态与生产构建 | format、lint、typecheck、build 通过；探针独立 format/lint 通过；应用尚未引入采集模块，因此另做探针的生产 bundle 构建 |
| 实际 Chromium 合成采集 | 16/24/48 kHz 均产出连续、非空、有限值的单声道 PCM；transfer 后仍可继续采集；输入非零期间通过 probe-only analyser 测得 worklet 输出峰值为 0 |
| 本地背压与取消 | 消费者永不完成触发 capture_overrun；启动取消及迟到授权处理通过；主页面 5 个 owned contexts 已关闭、其 streams 全部 tracks ended |
| 页面离开 | 父页面保留资源引用，真实导航同源 iframe；显式观察旧会话 stopped、tracks ended、context closed，不依赖 GC 推断 |
| 权限负例 | 浏览器真实执行 `Permissions-Policy: microphone=()` 拒绝，返回 capture_permission_denied 且关闭 context；不证明用户在系统/浏览器权限弹窗中的选择 |
| 网络与输出 | pageerror 0、非 loopback 页面请求 0；只保存计数/状态，不写音频内容 |

探针启用 Chromium fake-device 和 fake-UI，仅访问合成设备；另绕过 autoplay 策略以执行 headless 测试，因此不证明真实用户手势/自动播放资格。当前 Chromium 在没有 fake-UI 时，即使 Permissions API 返回 denied，原生 getUserMedia 仍先返回 NotSupportedError；不能将该路径伪装成真实权限拒绝通过。探针采用明确的页面权限策略负例，真实用户权限弹窗继续待验。

先观察到缺少模块的 RED，再实现通过。另有两个实际失败回归：取消发生在授权 promise fulfillment 与 stream 接管之间时泄漏 track；消费者 transfer 后用已分离 buffer.length 计算 ACK 得到 0。分别将 stream 接管放到原生 fulfillment 内、提前固定帧末水位后通过。权限 DOMException 归一化保留稳定错误分类，不输出浏览器原始设备细节。

独立只读审查未发现可确认的 P1/P2，复跑 41/41，并以内存转译和 host fake 检查两种 grant/abort 微任务交错。审查指出原探针未测实际输出振幅；随后补充 analyser 测量并重跑通过。图谱 list/coverage 确认本仓库未索引，当前工具面不能建索引；结构判断采用直接源码，未声称图谱验证通过。

`.runtime/team-c/c04-capture-probe.json` 保存核验时间、浏览器版本、时钟/计数、清理、导航及策略拒绝结果。探针结束后关闭浏览器和 loopback server，并清理自己创建的临时 bundle。全量 C 补丁和逐文件摘要继续保存在 `.runtime/team-c/team-c-current.patch` 与相邻 manifest，使用临时 index 打包，并在基线副本重放后核对字节。

## 剩余交付

- 真实麦克风/设备切换、真实权限与用户手势、Safari/跨浏览器、实际后台节流/主线程压力和长时质量尚未验证。host fake、合成 Chromium 和真实硬件分别取证。
- 公开 realtime 类型/validator、WS credit/gap/epoch、重连、字幕 reducer、服务端错误与停止证据仍待 A05/B04/B06.VAD 的确定制品，不手写替代协议。设备采样率到部署允许描述的选择需要生成边界消费层。
- 工作台实际采集 UI/会议/提词消费者尚未接入。后续消费者必须处理 `closed`、失败 reason、显式重新开始和有界持久化；不能因隐藏后自动重开麦克风。
- 恢复账本与水位契约、生产 HTTP/迁移/对象区及完整 C00–C08 继续按各交接文件推进；本单元不能关闭 C04/C08 或 Gate。
