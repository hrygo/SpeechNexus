---
title: "B03 默认网关的真实文件 ASR 实验"
status: in_progress
version: "0.1.0"
date: 2026-10-25
---

# B03 默认网关的真实文件 ASR 实验

2026-10-25 UTC（北京时间 10 月 26 日），在 #168 的代码
`997bd462bf16b78962f2cd3ddfe3d8bb21a8b8c8` 上，真实 Qwen3-ASR 1.7B 经默认
`create_app(settings=...)`、持久文件 worker、CPU VAD 和 MinIO 写出了私有结果。
未注入 factory、识别器、VAD 或数据库 adapter；网关实际通过回环 TCP 提交和查询。
这补齐了[默认装配](../architecture/file-asr-runtime.md)的真实模型实验，
不代表 #142 的公开业务链或生产资格已经完成。

## 固定输入与实验边界

- Linux ARM64 / GB10 单主机；Python 3.14.9、PyAV 19.0.2、NumPy 2.4.6、
  ONNX Runtime 1.31.0。复用独立 vendor 环境，没有修改共享 lockfile。
- 固定镜像为 `local/qwen3-asr-vllm:0.28.0-arm64`，image ID
  `sha256:61cc94fec25198aeefcd93b96088a8e8db106d61d834a36d3b89ba7bf5b10765`。
- ASR 上游 revision 为 `7278e1e70fe206f11671096ffdd38061171dd6e5`；
  按 #162 候选 `6c53e03` 的制品 pin 重新核对 10 个文件，大小与 SHA-256 全部匹配。
  CPU Silero 权重摘要为
  `1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3`。
- bf16、eager、单并发、最大长度 8192；最终模板显式固定 KV cache 为 1 GiB，
  Memory/MemorySwap 均为 12 GiB。峰值与稳态预留保持相等，没有签发容量或降额资格。
- 每次使用唯一 node/instance/模板及 Docker 内部网络，没有发布引擎宿主端口。
  保持请求、输出和 access log 关闭；最终模板仅开启汇总统计用于核对成功推理数。
- 输入为 espeak-ng 合成英文句及 4 秒静音，没有读取私人音频。控制器使用临时随机根密钥，
  网关只接收相同目标的派生 key。错误 Bearer 返回 401；本轮没有验证真实旧 generation 的 key。
- PostgreSQL 为一次性 Unix-socket-only 集群，MinIO 为项目隔离实例，使用临时 schema、
  最小网关角色和版本化桶。复用集成 fixture 预置关系数据，**未调用 A 的生产准入或注册
  权威 binding/default/manifest**；这些实验行不能作为可发布配置。

## 启动失败与配置修正

保留四次运行记录，未将前面的失败合并成成功：

| 运行 | 结果 |
| --- | --- |
| `8ebdcbc327ac` | 准备阶段引擎以 1 退出；未抓取到引擎日志，原因未定位。控制器确认退出和移除 |
| `f061ba0d58d6` | 两次真实预热完成，耗时 177.788 秒；实验驱动读取不到推理计数，尚未进入网关识别 |
| `00da81d2d23d` | 保留旧 0.05 内存比例时启动失败：实际可用 KV cache 为约 0.85 GiB，8192 长度需要约 0.88 GiB |
| `1e4b3c806669` | 固定 1 GiB KV cache 后，两次真实预热及下述网关实验通过 |

在该固定镜像源码中，`disable_log_stats` 同时使统计收集关闭。因此最终实验模板移除
`--disable-log-stats`，继续关闭请求、输出和 access log。这是观测配置修正，
不能把第二轮未执行的识别记成失败或通过。

第三轮日志直接说明了旧内存比例的边界不足。最终模板增加
`--kv-cache-memory-bytes 1073741824`，保留原最大长度和 cgroup 上限；
该固定 vLLM 参数优先于按比例推导 KV cache。没有降低支持长度或停止其他服务。
这只是本配置的一次成功实验，未证明长期启动可靠性、全负载容量或任意主机适用。

## 最终结果

最终受控实例完成两次 20 秒合成窗口预热，用时 **132.827 秒**，随后执行：

| 场景 | 提交到私有结果可读 | 原生成功推理计数增量 | 结果 |
| --- | --- | --- | --- |
| 合成语音 | 0.542032 秒 | 1 | 非空；忽略大小写及标点后的单词序列与参考句一致 |
| 4 秒静音 | 0.147038 秒 | 0 | 空文本 |

两例各在独立实验 schema/桶内执行。每例均验证：

- 首次提交按原 intent 接收；同进程重放读取原身份。
- 关闭并重启默认网关后，读取相同 staging 回执，保留相同 engine request ID；
  重放状态为 `unknown_quarantined`，没有把原执行重新排队。
- 重启及重放后原生成功推理计数没有增加；每例仅有一个私有结果对象版本，
  固定版本读回且 request digest 匹配。
- 关系数据库中的预算计数未改变，`execution_stop_evidence` 仍为零。
  worker 成功、HTTP 返回及线程关闭均没有被转成 GPU 停止证据。

成功推理计数证明相应的完成数量，不能排除所有失败的网络尝试，也不替代
确认丢失、真实进程故障或多消费者竞争的专门验证。上述耗时不是纯推理时间、
P95 或吞吐资格；两个样本不构成真实语料质量结论。

## 停止、清理与证据

最终控制器 stop 用时 **6.039 秒**，exit code 为 0；原 stop intent 的检查点确认
实例退出与删除后资源释放，唯一目标没有遗留。临时 PG 已确认停止，
临时版本化桶经 fixture 清理，Docker 网络已删除。四轮均未停止受保护的 35B；
其 `StartedAt` 一直为 `2026-10-23T03:49:18.877205405Z`。

控制器双事实尚未接成供 A 核验的完整执行终结与预算释放链，
**未向网关登记 `VerifiedStopEvidence`，未退预算**。不能从实例被删除推断 A 已结算。

本机忽略目录 `.runtime/probes/default-real-asr/` 保留私有实验驱动、四份原始
`report-<运行>.json`、制品校验及脱敏诊断。它是本机证据入口，不是其他主机已验证的部署命令。
最终报告与驱动的固定摘要：

| 内容 | SHA-256 |
| --- | --- |
| `report-1e4b3c806669.json` | `d57789559c67dd2e5a072e1c173d377b44540c4a4a1ffb4774431caa8b72dc12` |
| `probe.py` | `cc395054441403381ebeaed3c2a3956563a41e5a0c7fd1375550f869639ec1a4` |

`validated-summary.json` 将原始报告命名不准确的
`wrong_generation_credential_rejected` 映射为 `wrong_bearer_rejected`；
原始测试只发送错误 Bearer，没有发送从旧 generation 实际派生的 key。
原报告保留不改写，摘要注明这一范围修正。

## 仍需交付

#168 尚未主线集成。A 的真实完整准入、权威配置、Job、发布与预算结算，
公开 SDK/C 消费、真实取消/期限/迟到、提交或发布确认丢失、引擎/控制器故障、
真实双消费者及物理多主机资格均未由本轮证明。后续先将本次固定配置和私有结果
交接到同版 A/B/C 入口，再完成可信停止/预算和 CustomVoice TTS；#142 与 B00–B07 保持未完成。
