---
title: 资格报告 GB10 历史样例与任务文件清单
status: archived
date: 2026-10-28
---

# 资格报告 GB10 历史样例与任务文件清单

以下保留原任务时点记录，不能作为当前部署资格；持续使用的规则见[资格报告手册](../../operations/qualification-report.md)。

## 8. 真实样例：GB10 / Qwen3-ASR-1.7B

### 8.1 样例身份

| 项 | 值 |
| --- | --- |
| `report_id` | `qrep-file-asr-qwen3-1p7b-gb10-20261027` |
| `binding` | `file-asr-qwen3-1p7b` revision `1` |
| `binding_revision_id` | `mbrev-file-asr-qwen3-1p7b-gb10-20261027`（提议值） |
| `capacity_manifest_id` | `capm-file-asr-qwen3-1p7b-gb10-20261027` |
| `release_tier` | `experimental` |
| `topology` | `cube-ccba`, `arm64`, `nvidia-gb10-single-node`, `unified_memory`, 130,631,430,307 B |
| `software` | app `0.1.0.dev0`, contract `1.0.0`, one `vllm` stage, runtime `vllm-0.28.0+py3.12.3` |
| `test_suite` | revision `1`, corpus `audio-corpus-20261024`, 14 scenario ids |
| `required_scenarios_passed` | `false` |
| `overall_status` | `incomplete` |
| `evidence_digest` | `sha256:082bd34a404c811d179c982df5f9fab1e6dc39f9e495a1b824da67708549d634` |
| `qualified_at` / `expires_at` | `2026-10-27T20:44:19Z` / `2027-01-25T20:44:19Z` |
| 报告文件 SHA-256 | `sha256:e4f3900bd171eb58c9ac91650f64f2e57b556737f3d715893debc8b210c9cd37` |

### 8.2 逐场景结果

| scenario | required | status | 关键指标 | 来源/边界 |
| --- | --- | --- | --- | --- |
| `asr-fleurs-zh-baseline` | true | passed | CER 0%；latency 421 ms；audio 10.38 s；样本 1 | 转述；单次观测，不是 P95 |
| `asr-long-audio-60-280s` | true | passed | 最长 279.5 s；max latency 1871 ms；截断 0；样本 5 | 转述；拼接朗读，未触上下文上限 |
| `asr-narrowband-8k` | false | passed | CER 0%；max latency 417 ms；样本 2 | 转述；重采样/带限，不是真实电话 |
| `asr-fleurs-25-distinct` | false | passed | 25 样本 / 25 不同输出 / distinctness 1.0 | 转述；反模板复读，不是质量 |
| `asr-aishell4-farfield` | false | passed | max CER 4.6%；max latency 690 ms；样本 3 | 转述；AISHELL-4 远场，不是产品麦克风 |
| `asr-ascend-code-switch` | false | passed | 拉丁词保留 100%；语义替换 1；latency 585 ms | 转述；单样本，不是混说准确率 |
| `asr-babble-snr` | false | failed | 目标句保留 66.7%；样本 3（10/5/0 dB） | 转述；0 dB 改 1 个字，babble 内容泄漏 |
| `asr-overlap-two-speaker` | false | failed | 说话人分离 0%；样本 1 | 转述；混合/内容丢失，无时间戳/说话人 |
| `asr-silence-and-noise` | false | passed | 空文本率 100%；latency 128 ms；样本 1 | 转述；单样本白噪声 smoke |
| `asr-gateway-file-e2e` | true | skipped | — | 未执行；全部直连引擎，网关/持久化/超时/重试未覆盖 |
| `asr-cancel-and-abort` | true | skipped | — | 未执行；abort/停止证明/quarantine/预算释放未覆盖 |
| `asr-concurrency-and-throughput` | true | skipped | — | 未执行；串行请求，11.24x 只是 KV cache 算术上界 |
| `asr-real-world-noise` | false | skipped | — | 未执行；无街道/餐厅/产品设备录音 |
| `asr-dialect-and-accent` | false | skipped | — | 未执行；无标注方言/口音语料 |

报告 `limitations` 共 10 条，覆盖：只测直连引擎、取消未验、无并发/P95、CPU/PCM/scratch/带宽是声明、12 GiB 是 cgroup 上限、8 kHz 是派生、babble 合成、方言/口音/真实噪声未测、279.5 s 上限、binding/instance/publication 尚未真实存在且摘要需重算。

### 8.3 实测 / 转述 / 未验证

| 类别 | 内容 |
| --- | --- |
| **本任务实际执行** | 读取报告/证据/pin、校验 schema、计算摘要、运行一致性脚本正例/负例、检查报告状态计算。没有 GPU 测量，没有重新下载模型。 |
| **他人实测、本文转述** | 上表所有质量/延迟/时长/样本数、模型加载 18.4 s、KV cache 2.34 GiB、11.24x 算术上界、镜像 digest 等。来源是 #243 comment `6453106316`；原始 `DEL-103-evidence.md` 未提供。 |
| **仓库静态声明** | artifact pin 的模型/文件 digest、license、runtime pin、镜像名；这些是 pin 记录，不等于本任务重新核验了权重字节。 |
| **未验证** | gateway E2E、artifact 持久化、correlation/timeout/retry、cancel/abort/quarantine/预算释放、并发/P95/持续吞吐、真实噪声/产品麦克风/电话信道、方言/口音、峰值内存、CPU/PCM/scratch/带宽、上下文极限、生产容量/故障切换。 |

## 12. 文件清单（本任务新增/修改）

| 文件 | 作用 |
| --- | --- |
| `docs/operations/qualification-report.md` | 本文 |
| `docs/operations/capacity-manifest.md` | 配套 manifest 定义、DB 一致性、脚本用法 |
| `deploy/engines/file-asr-audio/evidence/gb10-20261027/qualification-report.json` | 样例报告 |
| `deploy/engines/file-asr-audio/evidence/gb10-20261027/evidence.json` | 逐场景来源 |
| `deploy/engines/file-asr-audio/evidence/gb10-20261027/capacity-manifest.json` | 配对 manifest |
| `deploy/engines/file-asr-audio/evidence/gb10-20261027/binding-definition.candidate.json` | 未来 revision 候选定义（非部署） |
| `deploy/engines/file-asr-audio/evidence/gb10-20261027/README.md` | 样例包说明与摘要 |
| `tests/fixtures/release-evidence/gb10-20261027-db-snapshot.json` | 一致性规则 fixture |
| `scripts/dev-env/verify-release-evidence.py` | 只读一致性校验 |
| `docs/archive/operations/release-evidence-verification-output.txt` | 实际运行原始输出 |

## 原手册的采样、阈值及适用范围记录

样例的采样事实：

| 场景 | 样本 | 采样边界 |
| --- | --- | --- |
| FLEURS baseline | 1 段 10.38 s | 干净朗读；CER 0；latency 单次 421 ms，不是 P95 |
| 长音频 | 5 段，62.84–279.5 s | 25 段 FLEURS 拼接 + 400 ms 静音；朗读语音，不是自发长语音 |
| 8 kHz | 2 个变体 | 直接重采样 + 300–3400 Hz 带限；不是真实电话录音 |
| FLEURS 25 distinct | 25 段 | 25 个不同输入产生 25 个不同输出；反模板复读，不是质量指标 |
| AISHELL-4 远场 | 3 段 | 公共远场会议数据，不是产品麦克风/房间 |
| ASCEND 混说 | 1 段 | zh-en 混说；只证明拉丁词形保留与 1 个语义替换，不是混说准确率 |
| babble | 3 个 SNR（10/5/0 dB） | 4 说话人合成 babble；0 dB 失败 |
| 重叠 | 1 段 | 两个 FLEURS 说话人等 RMS 叠加；无分离/时间戳 |
| 静音/噪声 | 1 段 8 s 白噪声 | 只证明该样本返回空文本，不是广泛幻觉资格 |
| gateway E2E / cancel / concurrency / real noise / dialect | 0 | 未执行；见 §6 |

样例的阈值例子：

- baseline：标点归一化后 CER = 0%；
- 长音频：5 个时长全部 HTTP 200、包含最后一段、无截断；
- 8k：两个变体 CER = 0%；
- 25 distinct：25 个输出全部不同；
- AISHELL-4：每个样本 CER < 5%；
- ASCEND：所有拉丁词保留表面形式，且唯一语义替换被披露；
- babble：10/5/0 dB 都必须保留完整目标句；0 dB 失败；
- 重叠：必须分离说话人；当前 0%，失败；
- 静音：8 s 白噪声返回空文本（单样本 smoke）。

### 6.1 失败模式（样例已记录）

| 失败/限制 | 事实 | 影响 |
| --- | --- | --- |
| babble 0 dB | 目标句保留率 66.7%（3 个 SNR 中 0 dB 改了 1 个目标字）；babble 说话人内容泄漏到输出 | 强噪声下不能承诺逐字保留；需要降噪/分离或产品降级 |
| 双人重叠 | 说话人分离 0%；内容混合/丢失；无时间戳/说话人标签 | 不能用于会议/多人重叠转写 |
| 8 kHz | 仅是重采样/带限干净语音 | 不能等同于真实电话信道 |
| 长音频 | 最长 279.5 s，拼接朗读；上下文上限未触达 | 超过该时长/自发长语音未知 |
| AISHELL-4 | 3 个远场样本，非产品麦克风/房间 | 不能外推到产品设备 |
| ASCEND | 1 个样本；1 个语义替换 | 不是混说准确率 |
| 静音/噪声 | 1 个白噪声样本 | 不是广泛幻觉资格 |
| gateway/cancel/concurrency | 未执行 | 公开网关、持久化、超时/重试、取消/隔离、并发/P95 全部未证明 |
| 资源峰值/带宽 | 12 GiB 是 cgroup 上限；CPU/PCM/scratch/带宽未观测 | 不能把声明当实测；容量证据仍是实验级 |

### 6.2 适用范围

| 维度 | 已覆盖 | 未覆盖 |
| --- | --- | --- |
| 语言 | FLEURS zh；ASCEND zh-en 混说 | 方言、口音、其他语言（定义虽支持 `auto/zh/en`，但没有相应资格） |
| 口音 | 无 | 方言/口音未测 |
| 噪声 | 合成 babble、白噪声、AISHELL-4 远场 | 真实街道/餐厅/产品设备噪声 |
| 信道 | 16 kHz 干净语音；8 kHz 派生 | 真实电话、产品麦克风、远场阵列之外的房间 |
| 时长 | 10.38 s baseline；62.84–279.5 s 长音频 | >279.5 s、上下文极限、自发长语音 |
| 并发 | 无（串行） | 并发、P95、持续吞吐、KV cache 实际上限 |
| 取消/隔离 | 无 | abort、停止证明、quarantine 恢复、预算释放 |
| 网关 | 无（全部直连引擎 127.0.0.1:8100） | correlation、artifact 持久化、timeout、retry |
| 输出 | 文本/分段 | 时间戳、说话人、对齐 |

## 原任务后续建议

5. **gateway/cancel/concurrency 是 required 但未执行**：这是当前 `incomplete` 的主因。后续卡应先在隔离环境补这三类证据，再谈生产。
6. **资源峰值/带宽未测**：12 GiB 是 cgroup 上限；CPU/PCM/scratch/带宽是声明；建议补资源高水位测量，尤其是 `host_pcm_buffer_bytes` 和 ingress/egress。
7. **原始主记录未提供**：`DEL-103-evidence.md` 未随任务提供；本报告只能标“转述”。建议证据接收人补原始记录或明确其保留位置，否则不能升级为独立复核。
