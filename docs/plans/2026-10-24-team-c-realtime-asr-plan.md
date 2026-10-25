# 公开实时ASR消费与字幕页面 Implementation Plan

> 执行采用已授权的 executing-plans inline 模式。主代理实施，整个单元一次 fresh 终审，不提交/推送。

**Goal:** 让正式工作台消费公开ASR会话与事件，按协商credit发送有界PCM，区分预览/正文/整体终态，并在断线和停止未知时保留真实状态。

**Architecture:** 从A生成入口导入REST/事件类型与校验；ASR reducer负责语义，client负责同源REST/WS，journal/session负责原意图、设备和生命周期，React只展示快照。真实生产A05/B04未就绪时明确不可用，开发合成服务不注入正式遥测。

**Tech Stack:** 现有React/TypeScript、生成speech client/realtime parsers、AudioWorklet MicrophoneCapture、Vitest、Playwright及test-only loopback HTTP/WS。

**Spec:** [语音API §2](../architecture/speech-api.md#2-实时协议与状态机)、[源事件契约](../../contracts/realtime-events.schema.json)、[REST契约](../../contracts/speech-openapi.yaml)、[十条共享语义序列](../../tests/fixtures/contracts/transcription-sequences.json)，C00 #35 / C04 #39 / C08 #43。

## Global Constraints

- 当前base/head为9eea85987232eaebbcbd6768c46fe0a084a4d177；279文件包74bef065先逐字节复核并先更新九issue。保留原改动，不改A/B owner、机器契约/生成器或生产组合根。
- 此单元完成ASR消费者；TTS/双工与真实资格继续在完整Goal中，不将本单元当作C04或Gate完成。
- 同源REST/WS，session_id仅路由；禁止任意端点、凭据/URL token、重定向。固定24kHz mono PCM16LE，每帧/JSON/credit及网络积压都有硬上界，不持久化PCM。
- 只在session.created+匹配nexus.ready后开放采集。断开/隐藏/离开停止设备，创建新会话才续接并显式gap；无透明PCM重放或旧结果串入。
- completed可为空，不能被迟到preview/failed/矛盾completed改写；正文成功不代表含required辅助的Operation成功，清理事实独立。
- sessionStorage先保存原创建/取消key和body，损坏/外部改写/quota阻断首次与重放。只恢复元数据/原意图与有界最终正文草稿（不保存preview或PCM），刷新后不自动连接/采集；本tab草稿不等于Meeting/Artifact已保存。
- 浏览器只用合成麦克风/合成服务；不操作真实设备、模型或生产服务。没有新跨chat消息授权。

## Review Focus

1. 重复event_id/changed内容、旧epoch、迟到final或required辅助失败：最终正文和整体执行分层，不错挂辅助。
2. 不足credit、超大JSON/PCM、ACK倒退或超sent：不虚构确认、不丢音装静音，设备/内存有界。
3. 创建/取消已提交丢回执及存储前后失败、外部改写：原键原body可恢复，未知不替换，不误报算力释放。
4. 隐藏/导航/卸载/WS关闭和异步getUserMedia/GET迟到：释放本会话资源，旧回调不更新新会话，不自动重播。
5. 字幕长文本、空成功、无服务、手填部署引用、窄窗口/键盘：明确成功范围、共享部署和gap，纯文本显示，不伪造ready。

## Task 1: ASR reducer、PCM和共享序列

Files: `web/workbench/src/realtime/asr-stream.ts`、`pcm.ts`及其测试/合成fixtures。

Interfaces: `AsrStream`消费生成RealtimeEvent及已固定会话引用；`receive/command/disconnect/getSnapshot`分别处理服务事实/本地命令/传输缺口/不可变快照。`encodePcm`将有限Float32帧转换到小端PCM16并经生成append事件校验。

- [x] 写十条共享序列正负语义测试及nexus ready/epoch/seq/水位、preview替换/空final/required失败/辅助版本、PCM/credit上界的失败测试，Expected: RED。
- [x] 实现纯语义边界与有界状态；协议错误保留已确认正文，不产生虚假终态。Expected: focused GREEN。

## Task 2: 原意图、同源客户端与媒体生命周期

Files: `realtime-client.ts`、`realtime-journal.ts`、`realtime-session.ts`及测试，复用media/MicrophoneCapture。

Interfaces: 生成REST create/get/cancel；`connectionUrl`只接受同源 `/v1/realtime?session_id=...`；journal闭合元数据/pending；session显式create/replay/connect/startCapture/commit/close/refresh，已有tab刷新后不自动恢复PCM。

- [x] 先测试错误URL/身份、创建/取消未知、每次dispatch存储屏障、停止与旧generation/设备迟到；Expected: RED。
- [x] 连接事件先校验后reducer，协商credit与WS缓冲限制，事件错误/断线关闭采集；用户显式GET或session.closed后读取权威清理事实，原接受回执不代替当前GET。Expected: focused GREEN。

## Task 3: 正式页面、真实合成浏览器与交接

Files: `RealtimeHost.tsx` / CSS /测试及App入口；test-only HTTP/WS与Playwright probe；计划、交接、README双语和Goal指针。

- [x] 先写入口/状态/显式采集与关闭、字幕分层/gap/原键恢复交互失败测试，Expected: RED→GREEN。
- [x] 生产bundle真实Chromium通过loopback合成REST/WS和合成麦克风，覆盖回执丢失、credit/断线gap、预览替换/空final/required失败、停止未知与导航设备释放、360px/basic键盘、出站/异常0。
- [x] Web五门禁、28源契约/漂移；无Python/生成源码改动，复用仍有效证据。唯一fresh终审0 Critical/3 Important/1 Minor；一轮TDD修复I1–I3，4 RED→41 GREEN，Minor延期、7 Declined逐项裁决，无追加复审。
- [x] 累计295文件包干净apply/全部字节/本地链接/index保护，九issue状态/任务更新及逐字回读；最终哈希与回读证据见本单元runtime收尾记录，完整Goal/G0–G5继续active/open。
