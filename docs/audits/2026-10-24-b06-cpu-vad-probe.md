---
title: "B06 CPU VAD 合成探针"
status: in_progress
version: "0.1.0"
date: 2026-10-24
---

# B06 CPU VAD 合成探针

本轮实现和验证 [B06 #33](https://github.com/hrygo/SpeechNexus/issues/33) 的第一项原生 VAD
消费单元，基线 `7372778364872a0a2f9a4ae51b191b2804eee3ea`。完整接口见
[CPU VAD](../architecture/cpu-vad.md)。模型运行在当前 Linux ARM64 宿主 CPU，没有启动模型容器、
使用 GPU 或停止受保护 LLM。临时合成音频随探针退出删除，报告不保存正文、音频或模型路径。

这是一套 VAD 局部证据：未调用 ASR，未接公开路由、worker、完整配方准入或 A 的 runtime 循环；
不能据此关闭 #33、G1/G2/G3 或发布真实录音/实时容量资格。

## 固定环境

- Silero ONNX：来源 `snakers4/silero-vad` @ `bfdc0193023f121ea5b3cc7b176dbed570a68a59`；
  2,327,524 bytes，SHA-256
  `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`；MIT 文本已保留。
- Python 3.14.9；ONNX Runtime 1.31.0 / NumPy 2.4.6；只使用 `CPUExecutionProvider`，
  intra/inter-op 线程数均为 1。vendor 环境单独固定依赖，不改变共享应用锁文件。
- 配方 `cpu-vad-1`、config revision `probe-1`，开始/结束阈值 0.5/0.35；
  连续人声 96 ms、前缀最多 300 ms、结束静音 500 ms、最大段长 20000 ms。
- 策略摘要 `sha256:169a5438fa6a8b685b89f181cf0c6fce99c82c75440e2e516ecf44c118f8ba29`。

## 合成结果

| 输入 | 音频时长 | CPU VAD 总耗时 | ASR 候选段 | 结论 |
| --- | --- | --- | --- | --- |
| espeak-ng 固定英文句，前后补静音 | 6.1484 s | 0.014733 s | 1 | 正常切出人声；余尾为无语音结果 |
| 全零 PCM | 4 s | 0.009278 s | 0 | 原生门禁不提供可外发 ASR 的音频 |
| 440 Hz / 振幅 4000 的正弦 PCM | 30 s | 0.069025 s | 0 | 20 s rollover + 10 s 尾段，均为无语音 |

固定模型构造 0.076410 s，首个 32 ms 推理帧 0.000484 s。
进程最大 RSS 157,388 KiB 包括 Python、NumPy、合成与探针工具，并非可发布的实例预算或稳态峰值。
合成人声最大私有 PCM 缓冲观察为 167,936 bytes；两个无语音样本均为 10,496 bytes。

三个样本改用 100 ms 与 22.9375 ms 输入分块后，全部切段、PCM 和样本位置相同。
共享同一 ONNX session 的两个独立 detector 交错推理，并对另一 detector 反复 reset，
与独立顺序推理结果一致，误差容限 `1e-6`。这证明本模型/CPU 运行时的此项状态隔离，
不证明部署级并发容量。

最终报告位于忽略目录 `.runtime/probes/b06-vad/report-final.json`，运行输出
`/tmp/b06-vad-final-probe.log`。可重复命令见
[独立运行环境](../../deploy/engines/cpu-vad/README.md)；输入仅在本机生成。

## 自动化与交接边界

确定性测试覆盖概率滞回/去抖、前缀、最大段内精确切开与 PCM 无丢失、
长静音上限、onset 跨窗口、任意小输入块、尾帧补零、错误概率、
clear/close、模型状态隔离、摘要/CPU provider/输出校验与错误信息保护。
契约测试将原生关闭原因和样本投影校验到既有公开 schema，未新增或放宽机器协议。

本单元针对性验证为 58 passed。独立环境按生成的 requirements 哈希校验安装成功，包含
flatbuffers 25.12.19、packaging 26.3、protobuf 7.36.2；在该环境中直接加载固定模型并完成首帧
CPU 推理。安装日志 `/tmp/b06-vad-hash-install.log`。

rebase 到 `e0d8992aa7de98b22c5fee91cf33d3da5f646f8d`（含 #75/#76）后，
非集成测试 1564 passed，637 个 integration 用例未在该命令中执行，覆盖率 81.26%；
唯一 warning 为既有 SQLite ResourceWarning。Ruff format/check、mypy、7 项依赖边界、
28 份契约、绑定漂移、Compose config 和四个 Python 包构建均通过。
日志前缀 `/tmp/b06-vad-*-rebased.log`。此单元不改持久化，未在本机重复完整 PG/InnoDB/MinIO
集成套件；远端检查结果另记在交付 Issue/PR。局部门禁不能解读为完整产品已通过。

未验证：真实中文/多语言/噪声/音乐/低音量/极短人声、前缀与去抖的质量取舍、
实际 24→16 kHz 流式重采样与原文件时钟、请求级 ASR status/abort、
0.6B 预览与 1.7B full_segment、required 辅助完整配方、词级对齐、匿名分人、
进程失败后的公开 gap/新 epoch、物理集群与混合负载资格。VAD 正例不能证明正文正确，
VAD 阴性也不能作为任意真实音频“无语音”的绝对保证。
