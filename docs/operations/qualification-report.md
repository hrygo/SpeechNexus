---
title: 资格报告（发布证据）
status: active
version: "1.0.0"
date: 2026-10-28
---

# 资格报告（发布证据）

`qualification_report` 证明“某一个不可变 binding revision、某一组引擎镜像/运行时、某一组模型制品、某一套拓扑和契约版本，通过了声明的测试场景”。它不是模型排行榜，也不是“跑过一次就算生产可用”。发布命令必须同时收到 manifest id 与 report id；报告与 manifest 的 binding revision、release tier、时间、stage/engine 组件必须一致。

本文回答：

1. 每个字段是什么、由谁提供、如何核验；
2. 语料、采样、指标和阈值如何记录；
3. 失败模式、适用范围和有效期如何写；
4. 与 `artifact-pin.json` 的 digest 如何绑定；
5. 一个没参与过本项目的人，如何基于既有实测独立产出下一份合法报告。

本文不修改冻结契约、不修改产品代码，也不把 `local-test-capacity-1` 或“计划中的测试”写成已通过的资格。

## 1. 输入、输出与权威位置

| 角色 | 路径 | 说明 |
| --- | --- | --- |
| 冻结形状 | `contracts/qualification-report.schema.json` | 报告 JSON Schema 1.0.0 |
| 配对 manifest | `deploy/engines/file-asr-audio/evidence/gb10-20261027/capacity-manifest.json` | 报告引用的容量/执行声明 |
| 样例报告 | `deploy/engines/file-asr-audio/evidence/gb10-20261027/qualification-report.json` | 基于 #243 既有实测的报告；当前 `incomplete` |
| 原始证据转述 | `deploy/engines/file-asr-audio/evidence/gb10-20261027/evidence.json` | 逐 scenario 的结构化来源；报告摘要的输入 |
| 模型/运行时 pin | `deploy/engines/file-asr-audio/artifact-pin.json` | 模型文件、模型卡、VAD、运行时 pin；摘要关系的权威来源 |
| 候选定义 | `deploy/engines/file-asr-audio/evidence/gb10-20261027/binding-definition.candidate.json` | 只用于样例的“未来 revision 定义”；不是已部署定义 |
| 校验脚本 | `scripts/dev-env/verify-release-evidence.py` | 只读校验 schema、摘要、跨文档、DB 账本 |
| DB 快照 fixture | `tests/fixtures/release-evidence/gb10-20261027-db-snapshot.json` | 只证明查询路径；不是真实发布 |

**当前基线**：仓库 `main` 的完整 base SHA 为 `9d1b9bbdc1041bfb7c33505a4788490c1db5ea45`。样例中的测量全部来自 #243 comment `6453106316` 的 GPU 岗记录；原始主记录 `DEL-103-evidence.md` 未提供，因此本任务把数值标为**他人实测、本文转述**，而不是独立复现。

## 2. 顶层字段表

| 字段 | 必填 | 含义 | 谁提供 | 如何核验 |
| --- | --- | --- | --- | --- |
| `schema_version` | 是 | 固定 `"1.0.0"` | 报告作者 | JSON Schema；不得自造版本 |
| `report_id` | 是 | 报告不可变 ID | 证据作者 | `Identifier`；被 manifest `qualification.qualification_report_id` 和 publication evidence 引用 |
| `binding` | 是 | `{id, revision}`；不可变 binding 身份 | binding 草稿/发布系统 | `id`/`revision` 必须与 `speech_binding_revisions` 行一致 |
| `binding_revision_id` | 是 | 不可变 revision 的 `revision_id` | binding 草稿/发布系统 | 必须等于 manifest、DB revision、publication/evidence、instance |
| `capacity_manifest_id` | 是 | 配对 manifest 的 ID | 证据作者 | 必须等于 manifest `manifest_id`；不得写占位值 |
| `release_tier` | 是 | `experimental` / `best_effort` / `production` | 产品负责人；SM+PO 放行 | 必须等于 manifest；`production` 要求 required 全通过且有有效期 |
| `topology` | 是 | 主机、CPU、硬件类别、内存架构、总物理内存、共享父预算 | 平台/实验负责人 | 必须与 manifest `topology_ref`、`host_nodes` 对齐；拓扑摘要见 §7 |
| `software` | 是 | 应用版本、契约版本、每个 stage 的引擎组件 | 报告作者从 pin/镜像读取 | 组件 key 集合 = manifest stage id；engine kind 相等；image digest 与 evidence 一致 |
| `test_suite` | 是 | 测试套件 revision、语料 revision、scenario id 列表 | 测试/证据负责人 | `scenario_ids` 必须与 `scenarios[].scenario_id` 一一对应且无重复 |
| `scenarios` | 是 | 每个 scenario 的结果、指标、证据摘要、备注 | 测量执行人/转述人 | 状态只能是 passed/failed/skipped；每个 id 恰好一条 |
| `required_scenarios_passed` | 是 | 计算值：至少一个 required 且全部 required passed | 报告作者计算 | 必须与 scenarios 一致；脚本会重算 |
| `overall_status` | 是 | `pass` / `fail` / `incomplete`，只看 required | 报告作者计算 | fail 若有 required failed；pass 若 required 全 passed；否则 incomplete |
| `limitations` | 是 | 已知限制；非通过的可选/必需项必须记录 | 报告作者 | experimental 必须非空；每个 skipped/failed optional 至少一条限制；不能写“无”掩盖 |
| `evidence_digest` | 是 | `evidence.json` 原始文件字节的 SHA-256 | 证据作者 | 必须等于文件 SHA；不是 JSON 规范化摘要 |
| `qualified_at` | 是 | 资格完成时间（ISO 8601，带时区） | 测量/证据负责人 | 必须早于 `expires_at`；必须与 manifest 相同 |
| `expires_at` | 否（production 必须有） | 证据失效时间 | 证据负责人按政策 | 与 manifest 相同；过期即失效；`null` 只能用于非生产 |

## 3. 嵌套字段

### 3.1 `topology`

| 字段 | 含义 | 谁提供 | 核验 |
| --- | --- | --- | --- |
| `host_node_id` | 实际主机 ID，例如 `cube-ccba` | 平台 | 必须存在于 `host_nodes`，且 instance 的 `host_node_id` 指向它 |
| `cpu_arch` | `x86_64` / `arm64` | 平台 | 与主机/镜像架构一致 |
| `hardware_class` | 硬件类别，例如 `nvidia-gb10-single-node` | 平台 | 与 topology profile/采购记录一致；不是主机名 |
| `memory_architecture` | `unified_memory` / `discrete_device_memory` | 平台 | 与 manifest `topology_ref`、`host_nodes.memory_architecture` 相等 |
| `total_physical_memory_bytes` | 主机总物理内存 | 平台实测/系统信息 | 不得小于 `host_nodes.allocatable_*`；样例 130,631,430,307 B（约 121.66 GiB，按报告值换算，精度有限） |
| `device_memory_bytes` | 离散设备显存（可选） | 平台 | 统一内存架构通常不填或为 0；不要与 unified 双计 |
| `shared_memory` | 共享父预算 id 与共享角色 | 平台 | `parent_budget_id` 必须与共存实验记录一致；`shared_with_roles` 只能来自枚举 |

### 3.2 `software`

| 字段 | 含义 | 谁提供 | 核验 |
| --- | --- | --- | --- |
| `application_version` | 应用版本，例如 `0.1.0.dev0` | 构建/发布 | 与部署包版本一致；不是模型版本 |
| `contract_version` | 契约版本，例如 `1.0.0` | 仓库 | 与 `contracts` 版本一致 |
| `engine_components` | 每个 stage 一个对象；key 是 stage id | 引擎/平台 | key 集合 = `stage_graph[].stage_id`；不能多、不能少 |
| `engine_components[].engine_kind` | 引擎类型 | 引擎负责人 | 与 manifest stage `engine_kind` 相等 |
| `engine_components[].image_digest` | 引擎镜像摘要 | 构建/镜像仓库 | 与 evidence `software.engine_image_digest` 相等（单 vLLM 组件时脚本强校验） |
| `engine_components[].runtime_version` | 运行时版本，例如 `vllm-0.28.0+py3.12.3` | 引擎/平台 | 与 artifact pin 的 runtime pin / requirements 一致 |

### 3.3 `test_suite` 与 `scenarios`

| 字段 | 含义 | 谁提供 | 核验 |
| --- | --- | --- | --- |
| `test_suite.revision` | 测试套件版本（十进制字符串） | 测试负责人 | 变更场景/阈值必须递增 |
| `test_suite.corpus_revision` | 语料 revision/标识 | 测试负责人 | 必须是可追溯的语料版本；不能只写“FLEURS” |
| `test_suite.scenario_ids` | 场景 id 列表 | 测试负责人 | 与 `scenarios` 一一对应、无重复 |
| `scenarios[].scenario_id` | 场景 id | 测试负责人 | `Identifier`；与 suite 完全一致 |
| `scenarios[].required` | 是否门禁 | 产品/测试负责人 | required 至少一个；不能把失败场景改成 optional 来“通过” |
| `scenarios[].status` | `passed` / `failed` / `skipped` | 测量执行人 | skipped 不是 pass；必须有 notes/limitation |
| `scenarios[].metrics` | 指标数组 | 测量执行人 | 每个 metric：name/value/unit（可选 percentile 0–100） |
| `scenarios[].evidence_digest` | 该 scenario 在 evidence.json 中对象的 RFC 8785 SHA-256 | 证据作者 | 必须与 evidence 对象重算值相等；不是文件摘要 |
| `scenarios[].notes` | 通过/失败规则、采样限制、异常 | 测量/证据作者 | 必须写清阈值与边界；不能只写“OK” |

`Metric` 的 `unit` 只能是 `ms`、`s`、`bytes`、`percent`、`ratio`、`count`、`cpms`。`value` 是数字；没有 `threshold` 字段，阈值必须写进 `notes`，并在评审时人工核对。若后续需要机器可读阈值，应单独演进 schema，不能在本任务改冻结契约。

## 4. 语料、采样与阈值

### 4.1 语料

- 语料必须可追溯：数据集/仓库、revision/日期、语言/口音/信道、采样方式、样本数；
- 不能把“合成/派生”写成“真实”：样例的 8 kHz 是 FLEURS 重采样/带限，babble 是真实人声合成的背景，不是真实街道噪声；
- 真实音频、声音参考和私密内容留在部署网络，不进入报告、平台附件或公开仓库；报告只写元数据、文本指标和最小脱敏样例；
- 语料 revision 变化必须新报告；不能在同一 `report_id` 下悄悄换样本。

### 4.2 采样

每个 scenario 的 `notes` 至少写：

1. 样本数（`sample_count` 或文字）；
2. 时长范围；
3. 语言/口音/信道/噪声条件；
4. 是否单次观测、是否 P95、是否重复；
5. 通过/失败规则。

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

### 4.3 阈值

阈值不是 schema 字段；必须在 `notes` 里写“Pass rule / Fail rule”，并让非作者 reviewer 能独立重算。样例的阈值例子：

- baseline：标点归一化后 CER = 0%；
- 长音频：5 个时长全部 HTTP 200、包含最后一段、无截断；
- 8k：两个变体 CER = 0%；
- 25 distinct：25 个输出全部不同；
- AISHELL-4：每个样本 CER < 5%；
- ASCEND：所有拉丁词保留表面形式，且唯一语义替换被披露；
- babble：10/5/0 dB 都必须保留完整目标句；0 dB 失败；
- 重叠：必须分离说话人；当前 0%，失败；
- 静音：8 s 白噪声返回空文本（单样本 smoke）。

阈值必须有来源：产品 AC、场景 profile、或明确的研究问题。不能为了“让结果通过”事后改阈值；阈值变更要新测试套件 revision 和新报告。

## 5. 状态计算与生产门禁

```text
required = [s for s in scenarios if s.required]
required_scenarios_passed =
    len(required) >= 1 and all(s.status == "passed" for s in required)

if any(s.status == "failed" for s in required):
    overall_status = "fail"
elif required_scenarios_passed:
    overall_status = "pass"
else:
    overall_status = "incomplete"
```

规则：

- `skipped` 对 required 而言是**未完成**，不是通过；
- optional failed/skipped 不改变 `overall_status`，但必须出现在 `limitations`；脚本会给 `WARN`；
- `production` 要求 `overall_status=pass`、`required_scenarios_passed=true`、`expires_at` 非空；manifest 也必须是 `production`，且定义 `release_tier` 一致；
- `experimental`/`best_effort` 可以 incomplete，但不能用 incomplete 报告支撑生产发布；
- 报告与 manifest 的 `release_tier` 不一致即 `FAIL`。

样例：5 个 required（baseline、长音频、gateway、cancel、concurrency），2 passed，3 skipped → `required_scenarios_passed=false`，`overall_status=incomplete`。这是诚实结果，不能改成 pass。

## 6. 失败模式、适用范围与有效期

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

### 6.3 有效期

- `qualified_at` 必须早于 `expires_at`；样例为 90 天（2026-10-27 → 2027-01-25）；
- `production` 不得使用 `expires_at: null`；到期即失效，不能继续支撑发布；
- 失效事件：模型/权重/模型卡变化、引擎镜像/runtime 变化、拓扑/主机/内存架构变化、pool/service class/management mode 变化、stage graph/向量/限额/取消能力变化、语料/阈值变化、实测发现容量或质量退化、报告到期；
- 失效后必须新 report id（通常也新 manifest/revision），保留旧报告和原始输出；不能原地改已发布 JSON。

## 7. 与 artifact-pin.json 的 digest 关系

`artifact-pin.json` 是模型/运行时/辅助文件的 pin，不是报告的一部分；报告通过 manifest 和 evidence 间接绑定它。

| pin 字段 | 与报告/证据的关系 | 核验 |
| --- | --- | --- |
| `model` 对象整体 | RFC 8785 SHA-256 = manifest stage `model_revision_digest` | 报告本身没有模型 digest 字段；reviewer 必须通过 manifest 的 stage 与 pin 对照 |
| `model.repository` / `model.revision` | 模型身份 | 与 evidence `software.model_repository`/`model_revision` 对照 |
| `model.license` | 许可证（样例 Apache-2.0） | 报告不记录 license；发布检查清单/法务接受另行留痕 |
| `model.model_card.digest` | 模型卡文件摘要 | 与 pin/模型卡原始字节核对 |
| `model.files[].artifact.digest` | 权重/配置/词表文件摘要 | 本任务未下载模型，不能声称重算；发布前必须按制品存储核对 |
| `vad` 对象 | VAD 仓库/revision/ONNX artifact（样例 MIT） | 只有当 VAD 进入 stage graph 时才成为对应 stage 的模型证据；当前样例 stage graph 只有 ASR |
| `engine.runtime_pin` | 指向 runtime-pin.json | 报告 `runtime_version` 必须与 runtime pin/requirements 一致 |
| `engine.source_commit` / `distribution_inventory_digest` | 引擎运行时来源与 Python 分发清单 | 报告无对应字段；reviewer 必须查 pin 与镜像构建记录 |
| `repository_files[]` | 辅助文件（runtime pin、requirements、许可证）摘要 | 报告无对应字段；发布前按 pin 核对 |

报告的 `software.engine_components[].image_digest` 与 evidence `software.engine_image_digest` 必须一致；样例为 `sha256:61cc94fec25198aeefcd93b96088a8e8db106d61d834a36d3b89ba7bf5b10765`。`artifact-pin.engine` 没有直接存 image digest，因此这个值必须来自构建/镜像记录和 evidence，不能从 pin 猜。

报告 `evidence_digest` 是 `evidence.json` 的**原始文件字节** SHA-256；逐 scenario `evidence_digest` 是该 scenario 对象的 **RFC 8785** 摘要。两者不能混用：

- 文件摘要用于“整份证据没有被换过”；
- 逐场景摘要用于“这个 scenario 的数字/备注没有被改过”；
- 规范化摘要不能替代文件摘要，因为文件可以重新格式化而语义不变；
- 文件摘要也不能替代逐场景摘要，因为整份文件换掉但报告文字仍可能引用旧场景。

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

## 9. 从零产出下一份合法报告

1. **固定 binding/revision**：从草稿/发布系统取得 `binding.id`、`revision`、`binding_revision_id`；不要先写报告再补身份。
2. **固定 manifest**：manifest id、release tier、topology、stage graph 必须已存在；报告引用它，而不是另写一套容量。
3. **固定软件**：为每个 stage 记录 `engine_kind`、`image_digest`、`runtime_version`；镜像 digest 必须来自构建/镜像记录，并与 evidence 一致。
4. **固定语料**：写 `test_suite.revision`、`corpus_revision`、`scenario_ids`；真实音频只留元数据/脱敏文本，不进入报告或平台附件。
5. **逐场景执行/转述**：每个 scenario 写 `required`、`status`、`metrics`、`notes`（阈值/采样/边界）；转述必须标 `provenance=relayed_primary_measurement` 并记录来源。
6. **生成 evidence.json**：每个 scenario 一个对象；不要只写报告文字。为每个对象算 RFC 8785 摘要，填入 `scenarios[].evidence_digest`；为整个文件算原始字节 SHA，填入 `evidence_digest`。
7. **计算状态**：按 §5 计算 `required_scenarios_passed` 与 `overall_status`；不要手填与 scenarios 矛盾的布尔值。
8. **写 limitations**：每个 skipped/failed optional、每个未测维度、每个“声明不是实测”的值都要写；experimental 不得空。
9. **校验**：跑 schema + `verify-release-evidence.py`；`FAIL` 清零；`WARN/NA` 逐条解释。负例至少验证错误 manifest id/占位值会被拒绝。
10. **评审/发布**：非作者 reviewer 核对语料、阈值、失败模式、摘要；唯一 SM+PO 决定是否发布。发布时带 report id 与原始文件 SHA；发布后保存 publication/evidence 行和命令输出。

### 9.1 模板

```json
{
  "schema_version": "1.0.0",
  "report_id": "qrep-<binding>-<tier>-<yyyymmdd>",
  "binding": {"id": "<binding-id>", "revision": "<revision>"},
  "binding_revision_id": "mbrev-<...>",
  "capacity_manifest_id": "capm-<...>",
  "release_tier": "experimental",
  "topology": {
    "host_node_id": "<host-node-id>",
    "cpu_arch": "arm64",
    "hardware_class": "<hardware-class>",
    "memory_architecture": "unified_memory",
    "total_physical_memory_bytes": 0,
    "shared_memory": {"parent_budget_id": "<parent>", "shared_with_roles": ["speech", "workbench_llm", "auxiliary"]}
  },
  "software": {
    "application_version": "<app-version>",
    "contract_version": "1.0.0",
    "engine_components": {
      "<stage-id>": {"engine_kind": "vllm", "image_digest": "sha256:<image>", "runtime_version": "vllm-<...>"}
    }
  },
  "test_suite": {"revision": "1", "corpus_revision": "<corpus-revision>", "scenario_ids": ["<scenario-id>"]},
  "scenarios": [
    {
      "scenario_id": "<scenario-id>",
      "required": true,
      "status": "skipped",
      "metrics": [],
      "evidence_digest": "sha256:<rfc8785-scenario-digest>",
      "notes": "Pass rule: <...>; sample: <...>; boundary: <...>"
    }
  ],
  "required_scenarios_passed": false,
  "overall_status": "incomplete",
  "limitations": ["<why this is not qualified>"],
  "evidence_digest": "sha256:<raw-evidence-file-sha>",
  "qualified_at": "<ISO-8601>",
  "expires_at": "<ISO-8601-or-null>"
}
```

模板中的 `0`、空 metrics 和 `skipped` 是形状占位；真实报告必须按实际结果填写，不能把模板当通过证据。

## 10. 校验命令与输出

完整命令、依赖和 SQL 对照见 [capacity-manifest.md](capacity-manifest.md) §9。报告相关的最小运行：

```bash
UV_CACHE_DIR="$PWD/.uv-cache" \
UV_PYTHON=/opt/homebrew/bin/python3.14 \
uv run --no-project \
  --with 'jsonschema>=4.23,<5' \
  --with 'rfc8785>=0.1.4,<0.2' \
  python scripts/dev-env/verify-release-evidence.py \
  --manifest deploy/engines/file-asr-audio/evidence/gb10-20261027/capacity-manifest.json \
  --report deploy/engines/file-asr-audio/evidence/gb10-20261027/qualification-report.json \
  --evidence deploy/engines/file-asr-audio/evidence/gb10-20261027/evidence.json \
  --expect-manifest-id capm-file-asr-qwen3-1p7b-gb10-20261027 \
  --expect-report-id qrep-file-asr-qwen3-1p7b-gb10-20261027 \
  --artifact-pin deploy/engines/file-asr-audio/artifact-pin.json \
  --binding-definition deploy/engines/file-asr-audio/evidence/gb10-20261027/binding-definition.candidate.json \
  --database-snapshot tests/fixtures/release-evidence/gb10-20261027-db-snapshot.json \
  --require-published --require-digests --show-digests
```

脚本会检查：

- report schema；
- `test_suite.scenario_ids` 与 `scenarios` 一一对应；
- `required_scenarios_passed` / `overall_status` 重算；
- production gate 与 limitations；
- manifest/report/evidence 的 id、tier、topology、时间；
- stage 与 engine component 对应；
- `artifact-pin.model` 摘要、config bundle、topology、evidence 文件/逐场景摘要；
- `evidence_digest` 与文件 SHA；
- 对传入的 `--binding-definition` 与快照内定义分别执行同一组语义检查：`capacity_manifest_id` 存在且等于 manifest、release tier 可识别并一致、stage roles/tasks/max segment 对应（输出前缀分别为 `binding_definition.*` 与 `definition.*`）；
- live/快照 DB 的 revision/publication/evidence/instance/host/pool/workspace 账本。

样例正例输出 `RESULT PASS_WITH_WARNINGS`（optional 非通过、fixture source），负例把定义/证据改成 `local-test-capacity-1` 后输出 `RESULT FAIL`、退出码 `1`，并给出 `definition.capacity_manifest_id`、`definition.placeholder`、`database.evidence_identity`、`database.evidence_placeholder` 等失败项。原始输出见 `docs/operations/release-evidence-verification-output.txt`。

当前随仓 `deploy/engines/file-asr-audio/binding-definition.json` 不是可用发布定义：校验器会直接报
`binding_definition.capacity_manifest_id=FAIL`（missing）和
`binding_definition.release_tier=FAIL`（`production` vs manifest 的 `experimental`），随后仍报
`digest.config_bundle=FAIL` 与实例 `config_digest=FAIL`。这是定义与证据不一致的事实，不能通过修改
fixture 或放宽校验消除；应由绑定作者发布新的不可变 revision。

## 11. 已知缺口与后续卡建议

1. **无机器可读阈值**：`Metric` 只有 name/value/unit/percentile，没有 threshold；阈值在 notes 里。建议后续 schema 演进增加 `threshold`/`comparison`，本任务不改冻结契约。
2. **报告不记录 license**：license 在 artifact pin；发布检查清单/法务接受需单独留痕。不要往报告塞自由字段。
3. **报告不记录模型文件逐个 digest**：只在 artifact pin；reviewer 必须把 pin 与制品存储核对。本任务未下载模型，不能声称已核验权重字节。
4. **stage graph 与 VAD/对齐/分离**：当前样例只有 ASR stage；如果实际路径包含 VAD/对齐/说话人分离，必须新增 stage、manifest 和对应场景；报告不能只靠 `parameter_defaults` 推断。
5. **gateway/cancel/concurrency 是 required 但未执行**：这是当前 `incomplete` 的主因。后续卡应先在隔离环境补这三类证据，再谈生产。
6. **资源峰值/带宽未测**：12 GiB 是 cgroup 上限；CPU/PCM/scratch/带宽是声明；建议补资源高水位测量，尤其是 `host_pcm_buffer_bytes` 和 ingress/egress。
7. **原始主记录未提供**：`DEL-103-evidence.md` 未随任务提供；本报告只能标“转述”。建议证据接收人补原始记录或明确其保留位置，否则不能升级为独立复核。
8. **产品发布校验只检查 capacity manifest id**：发布 CLI 不校验报告完整性、摘要、DB 账本；脚本是补强，不是产品门禁。建议后续卡把关键检查接入发布路径。
9. **`local-test-capacity-1` 禁止进入真实路径**：测试可以用，但真实 manifest/report/definition/evidence 的 id 必须是稳定、可追溯的真实 ID；脚本会拒绝 `local-test-*`。

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
| `tests/fixtures/release-evidence/gb10-20261027-db-snapshot.json` | 查询路径 fixture |
| `scripts/dev-env/verify-release-evidence.py` | 只读一致性校验 |
| `docs/operations/release-evidence-verification-output.txt` | 实际运行原始输出 |
