---
title: "B03 有界媒体与真实录音输入探针"
status: in_progress
version: "0.1.0"
date: 2026-10-24
---

# B03 有界媒体与真实录音输入探针

本轮交付 [B03 #30](https://github.com/hrygo/SpeechNexus/issues/30) 的有界媒体与 ASR/VAD
原生输入单元；开工基线 `3b8052040111cdd8af3a50aaf5b0e8b3922e1f42`，
最终 rebase 至 `8c4ea9a`（含 #77/#79）。接口和限制见[文件媒体说明](../architecture/file-asr-audio.md)。
没有修改共享机器契约、业务存储或预算权威，没有接入真实 ASR/TTS 外发或公开路由。

## 环境与标准

Linux ARM64、Python 3.14.9；PyAV 19.0.2、NumPy 2.4.6、ONNX Runtime 1.31.0。
PyAV wheel 报告 libavutil 61.1.102、libavcodec/libavformat/libavdevice 63.1.102、
libavfilter 12.1.102、libswscale 10.1.102、libswresample 7.1.102。
固定 Silero 模型摘要
`1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`，
来源/许可证沿用 [B06 记录](2026-10-24-b06-cpu-vad-probe.md)。
配方 `pcm-decode-1` / `cpu-vad-1`，VAD revision `file-probe-1`，其余为缺省策略。

探针限制为 16 MiB 编码输入、60 秒解码时长、65,536 样本单帧和 30 秒合作式期限。
标准是完整 EOF、样本映射覆盖尾部、合成人声有候选、静音/纯音无候选。
真实录音只检查机械处理完成，不判定 VAD 准确率或识别质量。

## 真实 CPU 结果

| 输入 | 时长 | 解码 + VAD 耗时 | ASR 候选段 | 尾部 |
| --- | --- | --- | --- | --- |
| espeak-ng 合成人声 WAV | 4.760 s | 0.019073 s | 1 | 完整 |
| 同源 PCM16LE | 4.760 s | 0.014077 s | 1 | 完整 |
| 同源 MP3 | 4.760 s | 0.017222 s | 1 | 完整 |
| 同源 FLAC | 4.760 s | 0.016273 s | 1 | 完整 |
| 同源 OGG/Opus | 4.760 s | 0.019791 s | 1 | 完整 |
| 全零 PCM 的 WAV | 4 s | 0.013315 s | 0 | 完整 |
| 440 Hz / 振幅 4000 正弦 WAV | 30 s | 0.084623 s | 0 | 完整 |
| AISHELL-4 来源录音前缀 | 20 s | 0.058119 s | 4 | 完整 |
| AliMeeting 近场派生录音前缀 | 20 s | 0.057390 s | 1 | 完整 |

两段真实输入均为 16 kHz mono、320,000 源样本，取源录音起点 0。
完整源文件重新计算 SHA-256 并匹配上传索引后才截取；不是对所有 4,654 个文件的重新验证。
两段不能代表完整数据集、全部录音或质量覆盖。
源摘要与片段摘要、样本数留在本机报告；具体路径只在私有选择清单。
未读取或发布标注、转写、音频与声音特征。临时音频退出后删除，未发送 ASR 请求或使用 GPU。

进程最大 RSS 为 121,028 KiB，包括模型、Python、codec、合成和探针工具，
不是稳态峰值、长录音/并发资格或可归还的运行预算。
受保护 LLM 保持运行，容器启动时间仍为 `2026-10-23T03:49:18.877205405Z`。

首次真实探针因只选择小于 8 MiB 的完整 WAV 而未找到候选，明确失败。
检查索引后改为完整源摘要验证 + 有界临时前缀，重跑上述九例才通过。
没有将失败或未选中的录音计入通过数。

可重复命令见[独立运行环境](../../deploy/engines/file-asr-audio/README.md)。
最终报告 `.runtime/probes/b03-audio/report-final.json`、私有清单同目录；
运行日志 `/tmp/b03-audio-final-probe.log`。这些本机文件不入 Git。

## 自动化与未验证项

确定性测试使用合成 codec 音频与 VAD fake，覆盖五种格式、8–192 kHz、16/24 kHz 输出、
1/2/4/8 声道、重采样尾部、源 PTS/未知起点、绝对整数区间映射、错误声明、
资源超限、取消/期限、附属资源拒绝、读取放大、codec 清理及失败不生成成功摘要。
契约测试验证本地支持子集，不放宽内部执行命令；不下载模型或读取真实语料。

专项 **70 passed**；最终 rebase 后完整非集成回归 **1846 passed**、
637 integration 未在该命令执行，覆盖率 **81.97%**。
唯一 warning 为既有 SQLite 未关闭连接的 ResourceWarning。
Ruff format/check、mypy（291 文件）、七项依赖边界、28 份契约、生成绑定漂移、
Compose config、媒体工具链和四个 Python 包构建均通过。日志前缀 `/tmp/b03-audio-`。
本单元不改持久化，本机没有重复完整 PG/InnoDB/MinIO 集成；远端证据写入交付 Issue/PR。

独立目录 `/tmp/b03-audio-hash-runtime` 使用 `--require-hashes` 安装六项锁定依赖成功，
上述最终九例也在该独立环境执行通过。普通开发环境的 NumPy 版本不同，不将它误记为
部署锁版本；独立环境的安装和探针日志分别为 `/tmp/b03-audio-hash-install.log`、
`/tmp/b03-audio-final-probe.log`。

未验证：恶意 codec 的进程硬隔离、长文件/并发 RSS、真实 ASR request ID/status/abort、
交叠去重和正文/辅助 revision、TTS/编码输出、受控对象下载/摘要/发布接入、
A 的准入和预算返还、公开错误映射、完整中文/噪声/重叠人声质量、目标物理集群。
合作式取消不构成引擎停止或资源释放证据；B03/G1 和团队 B Goal 均保持未完成。
