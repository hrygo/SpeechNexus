---
title: "C04 浏览器采集实施单元"
status: active
date: 2026-10-24
---

# C04 浏览器采集实施单元

基线 `f210821d655bd7deb326825d750d83584c3ce159`。沿用 C04/C08：应用自己持有麦克风 track 和 AudioContext；停止、取消、权限/设备/处理器失败、隐藏及离开页面时释放。当前生成包未提供 realtime validator，本单元只定义本地 MessagePort/PCM 类型，不建立公开 WS DTO 或协议副本。删除恢复的外部账本/水位消费契约待 A07/C08 对齐，继续保持未完成。

## 不变量

- 使用显式请求的 AudioContext sampleRate，核对实际时钟相同；由浏览器音频图完成采样转换，不自行线性降采样。输入下混单声道，输出节点保持静音，避免回授。
- frameSamples 由消费者给出，范围 1–8192；未确认帧上限由消费者给出，范围 1–64。固定一帧缓冲；传出帧 transfer。额度耗尽立即终止并显式报告 capture_overrun，不能静默丢样或无界排队。
- 每次采集独立 token、连续样本水位、串行消费者和累计 ACK。迟到 token/已停止回调不得推进新采集或释放其额度。
- 默认启动期限 30 秒；用户可取消等待权限。取消后迟到的 getUserMedia 必须停止所有 tracks；模块加载/恢复上下文未完成也不能迟到连接。
- 只采集合成浏览器源；真实麦克风、Safari、后台压力、公开 credit/gap/epoch 和服务端停止证据仍需分别验收。

## Task 1：有界渲染线程生产者

创建 `web/workbench/src/media/capture-buffer.ts`、`capture.worklet.ts` 和相邻测试。先测试固定帧、跨 quantum、ACK、越界、无额度、NaN 与停止，再运行聚焦 Vitest 观察缺少实现失败；实现固定内存和明确失败，重跑通过。worklet 以最小 host fake 测实际模块，验证静音输出和异常终止。

## Task 2：采集生命周期与消费

创建 `web/workbench/src/media/microphone-capture.ts` 和测试。入口 `createMicrophoneCapture(options)` 消费显式 sampleRate/frameSamples/maxPendingFrames、onFrame 与可选 AbortSignal/deviceId，返回独立 session；本地 frame 带 token/startSample/Float32Array 和采样率。停止同时终止消费者 signal、tracks、port、节点和 owned context。先补权限拒绝、迟到授权、启动挂起取消、失败、顺序/背压、隐藏/离开/设备结束、重启隔离与无麦克风监听测试，逐步 RED→GREEN。

## Task 3：真实浏览器合成采集与交接

创建 `scripts/dev-env/workbench-capture-probe.mjs`，临时生产打包后使用 loopback Chromium synthetic getUserMedia。验证目标时钟、非空单声道连续帧、静音输出图、消费者背压、停止/取消/页面生命周期和 tracks/context 结束；阻止非 loopback 请求，输出只含计数/状态。分别记录 host fake、真实浏览器合成设备和未验证真实硬件。

运行 Web format/lint/typecheck/test/build、探针单独格式/lint、本地链接与 `git diff --check`。依据 executing-plans 做一次独立只读最终审查，必要修复先复现再验证。保留全部已有 C 改动，不 commit/push/外部消息或修改共享文件；打包补丁并重放校验，Goal 保持 active。

## Review focus

重点覆盖：主线程停顿而 render 继续、取消与权限成功同轮发生、消费者永不返回、浏览器 resume 未完成、track/context 在启动末尾结束。遇到这些情况必须有界且释放自身资源，不宣称设备/GPU 停止资格。
