# 实时TTS消费与播放 Implementation Plan

> 执行使用已授权的 executing-plans inline 模式；主代理实施，整个单元一次 fresh 终审，不提交或推送。

**Goal:** 工作台以固定profile/model/voice revision消费公开实时TTS，显式提交文本，展示真实网络与渲染水位，停止和刷新不重播旧音频。

**Architecture:** 复用生成REST/事件validator和同源client，TTS纯状态机只保留文本及水位，解码PCM直接进入有界AudioWorklet。独立journal保留文本、创建/关闭原意图，controller拥有socket/context/generation；React只展示快照。

**Tech Stack:** 现有React、TypeScript、Vitest、AudioWorklet、Playwright及test-only loopback合成REST/WS。

**Spec:** [语音API §2.5](../architecture/speech-api.md#25-tts-文本与音频的两种流)、[事件机器契约](../../contracts/realtime-events.schema.json)、[REST机器契约](../../contracts/speech-openapi.yaml)。C00 #35 / C04 #39 / C08 #43。

## Global Constraints

- base=head `9eea85987232eaebbcbd6768c46fe0a084a4d177`；旧295文件/03591dd8包先复核；保留其他工作，不修改A/B owner、生成物、锁文件或生产装配。
- 固定24kHz mono PCM16LE；请求/事件使用生成校验，profile/model/voice输入无虚构默认。浏览器无引擎凭据、任意端点或内容出站。
- 同一session仅一个未释放utterance；文本seq连续、冻结前缀不可更改；Operation终态、清理和播放完成独立。
- PCM不进入React快照、journal或查询缓存；队列不超过5秒，JSON不超过协商值/64KiB。断开/隐藏/导航停止本地播放，不重连或重播旧PCM。
- text_credit按剩余字符解释；客户端保守使用UTF-8字节预算且一次只保留一个未确认append，避免不明确字符单位导致超发。此选择降低非ASCII吞吐，待生产者明确单位后可精确放宽。
- 当前协议没有客户端输出ACK，不私造wire事件；有界本地播放不等于服务端停止/释放证据。物理声卡输出延迟不由worklet渲染水位证明。
- sessionStorage提交前CAS屏障、原键重放；文本可恢复但WS命令/音频不自动重放。ASR原行为保持。
- 合成服务/PCM和真实Chromium分层出证；TTS消费者完成不代表双工、生产服务、真实模型/物理设备/Linux/GPU或Gate资格完成。

## Review Focus

1. 文本ACK迟到、重复内容变化、旧epoch及错utterance/operation：不改冻结文本、不把旧音频播放到新generation。
2. Base64/PCM长度、chunk/sample缺口、超限及render故障：有界内存并立即停止，不能虚构播放完成。
3. 创建/关闭已接受但丢回执、quota/外部改写：原意图保留，首次和重放均经过CAS屏障。
4. 用户手势resume/addModule迟到、隐藏/导航/卸载：context/socket及时释放，不自动继续。
5. Operation成功但尚有缓冲、取消后迟到音频、空输出成功、窄屏/纯文本：区分网络与实际渲染，不把已取消音频复播。

## Task 1: 生成事件消费与有界TTS语义

Files: `web/workbench/src/realtime/tts-stream.ts`、`tts-fixtures.ts`及测试；`realtime-client.ts`及测试。

Interfaces: `TtsStream(session).receive(unknown)`返回本次已验证PCM块或null；`command(unknown)`在成功dispatch后记本地意图；`getSnapshot()`仅文本/引用/水位。`checkSession`匹配ASR/TTS各实际引用，REST client支持生成契约允许的两种单模态及双模态请求；ASR journal仍严格单ASR。

- [x] 先写文本seq/credit/冻结、重复/epoch、PCM/区间与唯一终态、引用匹配回归；运行focused，Expected RED。
- [x] 实现语义与有界PCM解码，运行focused，Expected GREEN。

## Task 2: 恢复、同源会话与播放生命周期

Files: `tts-journal.ts`、`tts-session.ts`、`tts-playback.ts`及测试。

Interfaces: journal保存严格元数据/文本/pending，controller显式create/replay/connect/start/append/commit/finish/cancel/close/refresh，playback拥有AudioContext/Worklet及render水位；原创建回执与GET当前事实分开。

- [x] 先测原键/存储屏障、受限WS、启动迟到/停止/导航、PCM超限/断线、成功未播完及取消；Expected RED→GREEN。
- [x] 本地播放初始化必须从用户动作启动；本地取消立刻静音，服务端清理仍待独立证据。Expected focused GREEN。

## Task 3: 正式入口、合成浏览器与交接

Files: `RealtimeTtsHost.tsx`、App/CSS/测试，`scripts/dev-env/workbench-realtime-tts-probe.mjs`，相关计划/交接/README。

- [x] 先写页面纯文本、显式操作/存储失败和状态分层测试，Expected RED→GREEN。
- [x] 生产bundle Chromium通过合成REST/WS/PCM与实际AudioWorklet验证文本/音频/渲染、取消/导航释放、原键恢复、360px/basic键盘和0出站/异常。
- [x] Web五门禁、契约漂移及影响范围检查；唯一fresh终审，Critical/Important一轮TDD修复，Minor记录延期。
- [x] 累计包干净apply/全部字节/链接/index保护、交接、九issue状态/任务同步及逐字回读；完整Goal继续active。
