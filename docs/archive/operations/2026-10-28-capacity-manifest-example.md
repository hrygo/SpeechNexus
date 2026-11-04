---
title: 容量 manifest GB10 历史样例与任务文件清单
status: archived
date: 2026-10-28
---

# 容量 manifest GB10 历史样例与任务文件清单

以下保留原任务时点记录，不能作为当前部署资格；持续使用的规则见[容量手册](../../operations/capacity-manifest.md)。

## 8. 真实样例：GB10 / Qwen3-ASR-1.7B

### 8.1 样例身份

| 项 | 值 |
| --- | --- |
| `manifest_id` | `capm-file-asr-qwen3-1p7b-gb10-20261027` |
| `binding_revision_id` | `mbrev-file-asr-qwen3-1p7b-gb10-20261027`（提议值，尚未在真实 DB 注册） |
| `pool_id` / `service_class` | `asr-batch` / `batch` |
| `management_mode` / `release_tier` | `external` / `experimental` |
| `topology_ref.host_node_profile_id` | `gb10-cube-ccba-unified-121g` |
| `topology_ref.topology_digest` | `sha256:1510168f01f3e4d3bb098b12dfd4ca33ee096239953b3fe8355e6eacb6c71aa4` |
| stage | `stage-qwen3-asr-1p7b-batch`，`asr_batch`，`vllm`，`max_active_slots=1` |
| 模型摘要 | `sha256:886652c5efb861de36ba18d46f58e9f9a7fb2d4469023264c992b055dd6ac1ca` |
| 配置包摘要 | `sha256:86626578d8937c895b27a346a9663dd45215bb9146f282d265f7a0c56de4d52e` |
| 资格期 | `2026-10-27T20:44:19Z` → `2027-01-25T20:44:19Z` |
| manifest 文件 SHA-256 | `sha256:269096a91dd4b4c09e6332bf490889c5e060be67510185c911218042e24ae149` |
| report 文件 SHA-256 | `sha256:e4f3900bd171eb58c9ac91650f64f2e57b556737f3d715893debc8b210c9cd37` |
| evidence 文件 SHA-256 | `sha256:082bd34a404c811d179c982df5f9fab1e6dc39f9e495a1b824da67708549d634` |

### 8.2 资源数值与来源

| 维度 / 限额 | 值 | 来源等级 | 说明 |
| --- | --- | --- | --- |
| `unified_physical_memory_bytes` resident/peak | 12 GiB = 12,884,901,888 B | **声明上界** | #243 的实验 cgroup 上限；不是观测到的峰值。报告限制明确写了“Peak resident memory was not observed” |
| `device_memory_bytes` / `host_memory_bytes` | 0 / 0 | 声明 | 统一内存架构，统一计入 unified，避免双计 |
| `cpu_decode_millicores` resident/peak/per-slot | 2000 / 3000 / 500 | **声明上界** | 没有 CPU 毫核实测；不能写成“实测” |
| `host_pcm_buffer_bytes` resident/peak/per-slot | 2 MiB / 2 MiB / 2 MiB | **声明上界** | 没有 PCM 高水位实测 |
| `scratch_disk_bytes` | 0 | 声明 | 本实验不使用 scratch；不是“测得为 0” |
| `ingress_bandwidth_bps` / `egress_bandwidth_bps` per-slot | 1,000,000 / 256,000 | **声明上界** | 没有带宽实测；host 也没有对应账本列 |
| `max_active_slots` / `realtime_reserved_slots` | 1 / 0 | 声明 | 单实例单 slot；没有并发实测 |
| `max_segment_audio_ms` | 30000 | 定义/产品 | 与 `binding-definition.json` 的 `parameter_domains.asr.max_segment_ms` 一致 |
| `max_prefill_tokens_per_sec` / `max_output_tokens_per_sec` | 1952 / 1952 | 声明 | 来自模型上下文/实验配置；不是吞吐实测 |
| `hard_output_quota` | true | 声明 | 依赖引擎先验限流；本报告没有验证硬配额执行路径 |
| cancellation | `connection_drop_with_quarantine`，abort/fencing=false，worst_case 10000 ms | **未验证** | 没有 abort/fencing 实测；不能声明 `engine_abort_confirmed` |

### 8.3 实测 / 转述 / 未验证

| 类别 | 内容 |
| --- | --- |
| **本任务实际执行** | 读取仓库文件、核对 schema、计算 JSON/文件摘要、运行校验脚本正例与负例。没有 GPU 测量。 |
| **他人实测、本文转述** | #243 comment `6453106316` 的 FLEURS 25 段、279.5 s 长音频、8 kHz 派生、AISHELL-4 远场、ASCEND 混说、babble/重叠/噪声、模型加载 18.4 s、KV cache 2.34 GiB、11.24x 算术上界。原始主记录 `DEL-103-evidence.md` 未提供。 |
| **仓库静态声明** | artifact pin 的模型/文件 digest、Apache-2.0/MIT 许可、镜像 digest、运行时候选版本。模型文件未在本任务下载，未重算权重字节。 |
| **未验证** | 网关 E2E、artifact 持久化、correlation/timeout/retry、取消/abort/隔离/预算释放、并发/P95/持续吞吐、真实街道/餐厅/产品麦克风噪声、方言/口音、真实电话信道、峰值内存、CPU/PCM/scratch/带宽、超过 279.5 s 的上下文行为、生产容量与故障切换。 |

### 8.4 当前样例的诚实结论

- 这是一份**实验级、未完成资格**的证据样例：5 个 required 场景中 2 个通过、3 个 skipped；`overall_status=incomplete`；不能用于 production。
- 12 GiB 是保守 cgroup 上限，不是实测峰值；CPU/PCM/scratch/带宽是声明上界；`resource_pools` 没有数值配额。
- 当前仓库 `binding-definition.json` 的 `execution_recipe` **没有** `capacity_manifest_id`，且 `release_tier=production`；与这份 experimental 证据不一致。真实发布前必须新建 revision，把候选定义中的 `capacity_manifest_id` 和 `release_tier` 落库，重算 `config_bundle_digest`，并重新资格。不能直接改历史定义或把 `local-test-capacity-1` 写入真实路径。

## 11. 文件清单（本任务新增/修改）

| 文件 | 作用 |
| --- | --- |
| `docs/operations/capacity-manifest.md` | 本文 |
| `docs/operations/qualification-report.md` | 配套报告定义 |
| `deploy/engines/file-asr-audio/evidence/gb10-20261027/capacity-manifest.json` | 真实数值样例 |
| `deploy/engines/file-asr-audio/evidence/gb10-20261027/qualification-report.json` | 配对报告 |
| `deploy/engines/file-asr-audio/evidence/gb10-20261027/evidence.json` | #243 结构化转述 |
| `deploy/engines/file-asr-audio/evidence/gb10-20261027/binding-definition.candidate.json` | 未来 revision 的候选定义（非部署） |
| `deploy/engines/file-asr-audio/evidence/gb10-20261027/README.md` | 样例包说明与摘要 |
| `tests/fixtures/release-evidence/gb10-20261027-db-snapshot.json` | 一致性规则 fixture |
| `scripts/dev-env/verify-release-evidence.py` | 只读一致性校验 |
| `docs/archive/operations/release-evidence-verification-output.txt` | 实际运行原始输出 |
