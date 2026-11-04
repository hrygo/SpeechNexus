---
title: 资格报告（发布证据）
status: active
version: "1.0.0"
date: 2026-10-28
---

# 资格报告（发布证据）

`qualification_report` 记录某一个不可变 binding revision、引擎镜像/运行时、模型制品、拓扑和契约版本的测试场景及其结果；报告也可以是 `fail` 或 `incomplete`，不能仅凭报告存在就认定场景通过。它不是模型排行榜，也不是“跑过一次就算生产可用”。通用 binding 发布命令必须收到 manifest id 与 report id，但不读取报告原文或自动验证两份制品：首次登记意图时核对 manifest id 与 binding 定义，并登记证据 ID 和可选摘要；重放不重新执行这项校验。报告与 manifest 的 binding revision、release tier、时间及 stage/engine 组件须由独立核验和评审确认，见本文末尾的边界说明。

本文回答：

1. 每个字段是什么、由谁提供、如何核验；
2. 语料、采样、指标和阈值如何记录；
3. 失败模式、适用范围和有效期如何写；
4. 与 `artifact-pin.json` 的 digest 如何绑定；
5. 一个没参与过本项目的人，如何基于既有实测独立产出下一份合法报告。

本文不修改冻结契约、不修改产品代码，也不把 `local-test-capacity-1` 或“计划中的测试”写成已通过的资格。

产品代码现提供 `speech.interfaces.binding_release_evidence.verify_binding_release_evidence`
作为只读接入组件：调用者从权威库取得精确 publication、发布证据及 binding 定义，另提供
同一份 manifest/report、scenario evidence 与 artifact pin 原始字节。每份输入最多 1 MiB，
拒绝重复 JSON 键、非有限数字和非法形状；manifest/report 必须与库中两个非空摘要匹配。
它复核固定身份、通过的 required 场景、场景摘要、时间、stage/engine 集合、资源聚合、
拓扑和配置摘要；实验等级的 incomplete 报告也不能作为通过结果。
返回值仅证明所读内容一致，不认证证据作者、不复跑测量、不检查实际模型文件、实例代次
或 speaker，也不签发声音资格。消费者仍须限定所需 release tier 并复核实际部署。
内建声音资格授权、签发及发布入口已调用该只读核验组件；具体调用边界见[内建声音资格](builtin-voice-qualification.md)。同步 TTS 工厂按配置及声音资格条件装配，见[同步语音合成](../components/synchronous-speech.md)；本组件不追溯改变历史资格，也不单独证明工厂可运行。

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
| DB 快照 fixture | `tests/fixtures/release-evidence/gb10-20261027-db-snapshot.json` | 用于验证快照上的一致性规则；不执行数据库查询，也不是真实发布 |

**样例基线**：编写时仓库 `main` 的 base SHA 为 `9d1b9bbdc1041bfb7c33505a4788490c1db5ea45`，不是当前主线承诺。样例中的测量全部来自 #243 comment `6453106316` 的 GPU 岗记录；原始主记录 `DEL-103-evidence.md` 未提供，因此本任务把数值标为**他人实测、本文转述**，而不是独立复现。

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
| `software` | 是 | 应用版本、契约版本、每个 stage 的引擎组件 | 报告作者从 pin/镜像读取 | 组件 key 集合 = manifest stage id，engine kind 相等；镜像摘要须逐 stage 核验，脚本只比较特定单镜像场景，见 §3.2 |
| `test_suite` | 是 | 测试套件 revision、语料 revision、scenario id 列表 | 测试/证据负责人 | `scenario_ids` 必须与 `scenarios[].scenario_id` 一一对应且无重复 |
| `scenarios` | 是 | 每个 scenario 的结果、指标、证据摘要、备注 | 测量执行人/转述人 | 状态只能是 passed/failed/skipped；每个 id 恰好一条 |
| `required_scenarios_passed` | 是 | 计算值：至少一个 required 且全部 required passed | 报告作者计算 | 必须与 scenarios 一致；脚本会重算 |
| `overall_status` | 是 | `pass` / `fail` / `incomplete`，只看 required | 报告作者计算 | fail 若有 required failed；pass 若至少有一个 required 且全部 passed；否则 incomplete |
| `limitations` | 是 | 已知限制；非通过的可选/必需项必须记录 | 报告作者 | 校验脚本要求非生产报告非空；每个 skipped/failed optional 的限制须人工核对；不能写“无”掩盖 |
| `evidence_digest` | 是 | `evidence.json` 原始文件字节的 SHA-256 | 证据作者 | 必须等于文件 SHA；不是 JSON 规范化摘要 |
| `qualified_at` | 是 | 资格完成时间（ISO 8601，带时区） | 测量/证据负责人 | 有 `expires_at` 时必须早于它；必须与 manifest 相同 |
| `expires_at` | 否（production 必须有） | 证据失效时间 | 证据负责人按政策 | 与 manifest 相同；过期即失效；`null` 只能用于非生产 |

## 3. 嵌套字段

### 3.1 `topology`

| 字段 | 含义 | 谁提供 | 核验 |
| --- | --- | --- | --- |
| `host_node_id` | 实际主机 ID，例如 `cube-ccba` | 平台 | 必须存在于 `host_nodes`，且 instance 的 `host_node_id` 指向它 |
| `cpu_arch` | `x86_64` / `arm64` | 平台 | 与主机/镜像架构一致 |
| `hardware_class` | 硬件类别，例如 `nvidia-gb10-single-node` | 平台 | 与 topology profile/采购记录一致；不是主机名 |
| `memory_architecture` | `unified_memory` / `discrete_device_memory` | 平台 | 与 manifest `topology_ref`、`host_nodes.memory_architecture` 相等 |
| `total_physical_memory_bytes` | 主机总物理内存 | 平台实测/系统信息 | 只能与同口径的内存预算比较，不能与 CPU、磁盘等其他 `allocatable_*` 维度比较；脚本仅对统一内存架构检查 `allocatable_unified_physical_memory_bytes` 不超过该值。样例 130,631,430,307 B（约 121.66 GiB，按报告值换算，精度有限） |
| `device_memory_bytes` | 离散设备显存（可选） | 平台 | 统一内存架构通常不填或为 0；不要与 unified 双计 |
| `shared_memory` | 共享父预算 id 与共享角色 | 平台 | `parent_budget_id` 必须与共存实验记录一致；`shared_with_roles` 只能来自枚举。该对象不在 `topology_digest` 的输入中，需另行核对；报告文件摘要仍固定其内容 |

### 3.2 `software`

| 字段 | 含义 | 谁提供 | 核验 |
| --- | --- | --- | --- |
| `application_version` | 应用版本，例如 `0.1.0.dev0` | 构建/发布 | 与部署包版本一致；不是模型版本 |
| `contract_version` | 契约版本，例如 `1.0.0` | 仓库 | 与 `contracts` 版本一致 |
| `engine_components` | 每个 stage 一个对象；key 是 stage id | 引擎/平台 | key 集合 = `stage_graph[].stage_id`；不能多、不能少 |
| `engine_components[].engine_kind` | 引擎类型 | 引擎负责人 | 与 manifest stage `engine_kind` 相等 |
| `engine_components[].image_digest` | 引擎镜像摘要 | 构建/镜像仓库 | 逐 stage 与对应构建、镜像和测量证据核对；脚本仅在 evidence 提供单个 `software.engine_image_digest` 且恰有一个 vLLM/vLLM-Omni 组件时比较该组件，否则标记 `NA`，须另行核验 |
| `engine_components[].runtime_version` | 运行时版本，例如 `vllm-0.28.0+py3.12.3` | 引擎/平台 | 与 artifact pin 的 runtime pin / requirements 一致 |

### 3.3 `test_suite` 与 `scenarios`

| 字段 | 含义 | 谁提供 | 核验 |
| --- | --- | --- | --- |
| `test_suite.revision` | 测试套件版本（十进制字符串） | 测试负责人 | 变更场景/阈值必须递增 |
| `test_suite.corpus_revision` | 语料 revision/标识 | 测试负责人 | 必须是可追溯的语料版本；不能只写“FLEURS” |
| `test_suite.scenario_ids` | 场景 id 列表 | 测试负责人 | 与 `scenarios` 一一对应、无重复 |
| `scenarios[].scenario_id` | 场景 id | 测试负责人 | `Identifier`；与 suite 完全一致 |
| `scenarios[].required` | 是否门禁 | 产品/测试负责人 | 要获得 pass 至少需要一个 required；没有 required 时状态为 incomplete。不能把失败场景改成 optional 来“通过” |
| `scenarios[].status` | `passed` / `failed` / `skipped` | 测量执行人 | skipped 不是 pass；必须有 notes/limitation |
| `scenarios[].metrics` | 指标数组 | 测量执行人 | 每个 metric：name/value/unit（可选 percentile 0–100） |
| `scenarios[].evidence_digest` | 该 scenario 在 evidence.json 中对象的 RFC 8785 SHA-256 | 证据作者 | 必须与 evidence 对象重算值相等；不是文件摘要 |
| `scenarios[].notes` | 通过/失败规则、采样限制、异常 | 测量/证据作者 | 必须写清阈值与边界；不能只写“OK” |

表中对 `notes` 的内容要求由证据评审执行；schema 将该字段设为可选，脚本也不检查文字是否包含阈值或采样信息。

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

历史实验的采样边界见[样例归档](../archive/operations/2026-10-28-qualification-report-example.md)。

### 4.3 阈值

阈值不是 schema 字段；必须在 `notes` 里写“Pass rule / Fail rule”，并让非作者 reviewer 能独立重算。历史实验的具体阈值见[样例归档](../archive/operations/2026-10-28-qualification-report-example.md)。

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
- optional failed/skipped 不改变 `overall_status`，但必须出现在 `limitations`；脚本会给 `WARN`，不会逐条验证限制文字是否对应每个场景，须人工核对；
- `production` 要求 `overall_status=pass`、`required_scenarios_passed=true`、`expires_at` 非空；manifest 也必须是 `production`，且定义 `release_tier` 一致；
- `experimental`/`best_effort` 可以 incomplete，但不能用 incomplete 报告支撑生产发布；
- 报告与 manifest 的 `release_tier` 不一致即 `FAIL`。

样例：5 个 required（baseline、长音频、gateway、cancel、concurrency），2 passed，3 skipped → `required_scenarios_passed=false`，`overall_status=incomplete`。这是诚实结果，不能改成 pass。

## 6. 失败模式、适用范围与有效期

### 6.1 失败模式与适用范围

报告应按语言、信道、噪声、设备、时长、并发、取消、网关和输出逐项记录已覆盖与未覆盖范围，以及失败对产品使用的影响。历史实验的具体记录见[样例归档](../archive/operations/2026-10-28-qualification-report-example.md)。

### 6.2 有效期

- 提供 `expires_at` 时，`qualified_at` 必须早于它；样例为 90 天（2026-10-27 → 2027-01-25）；
- `production` 不得使用 `expires_at: null`；到期即失效，不能继续支撑发布；
- 离线脚本检查截止时间是否已过，但不检查 `qualified_at` 是否晚于当前时间；产品只读 release 校验组件还要求 `qualified_at <= now`。脚本通过不能替代该时间核验；
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
| `vad` 对象 | VAD 仓库/revision/ONNX artifact（样例 MIT） | 实际使用时须核对 VAD 制品与许可证据；当前文件 ASR 将 VAD 作为消费者内部工作，样例 stage graph 只有 ASR |
| `engine.runtime_pin` | 指向 runtime-pin.json | 报告 `runtime_version` 必须与 runtime pin/requirements 一致 |
| `engine.source_commit` / `distribution_inventory_digest` | 引擎运行时来源与 Python 分发清单 | 报告无对应字段；reviewer 必须查 pin 与镜像构建记录 |
| `repository_files[]` | 辅助文件（runtime pin、requirements、许可证）摘要 | 报告无对应字段；发布前按 pin 核对 |

样例只有一个 vLLM 组件，其 `image_digest` 与 evidence 的 `software.engine_image_digest` 一致，均为 `sha256:61cc94fec25198aeefcd93b96088a8e8db106d61d834a36d3b89ba7bf5b10765`。多组件时应逐 stage 核对对应镜像及测量记录，不能把单个 evidence 摘要当作所有组件的证明。样例的 `artifact-pin.engine` 没有直接存 image digest，因此这个值必须来自构建/镜像记录和 evidence，不能从 pin 猜。

报告 `evidence_digest` 是 `evidence.json` 的**原始文件字节** SHA-256；逐 scenario `evidence_digest` 是该 scenario 对象的 **RFC 8785** 摘要。两者不能混用：

- 文件摘要用于“整份证据没有被换过”；
- 逐场景摘要用于“这个 scenario 的数字/备注没有被改过”；
- 规范化摘要不能替代文件摘要，因为文件可以重新格式化而语义不变；
- 文件摘要也不能替代逐场景摘要，因为整份文件换掉但报告文字仍可能引用旧场景。

## 8. 历史样例

GB10 / Qwen3-ASR-1.7B 的实验记录及原任务文件清单见[历史样例](../archive/operations/2026-10-28-qualification-report-example.md)；它不能证明当前部署资格。

## 9. 从零产出下一份合法报告

1. **固定 binding/revision**：从草稿/发布系统取得 `binding.id`、`revision`、`binding_revision_id`；不要先写报告再补身份。
2. **固定 manifest**：manifest id、release tier、topology、stage graph 必须已存在；报告引用它，而不是另写一套容量。
3. **固定软件**：为每个 stage 记录 `engine_kind`、`image_digest`、`runtime_version`；镜像 digest 必须与该 stage 的构建、镜像及测量证据一致。单个 evidence 镜像摘要不足以自动核验多个组件。
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

该模板是待填写的示意，并非有效的 schema 示例：`total_physical_memory_bytes` 的 `0` 不满足最小值 1，`<...>` 身份、摘要和时间也须替换为有效值。空 metrics 和 `skipped` 仅表示尚未完成的场景。`expires_at` 要替换为有效时间字符串，或在允许无有效期的非生产报告中替换为 JSON 字面量 `null`，不能填字符串 `"null"`。真实报告必须按实际结果填写，不能把模板当通过证据。

## 10. 校验命令与输出

完整命令、依赖和 SQL 对照见 [capacity-manifest.md](capacity-manifest.md) §9。报告相关的最小运行：

```bash
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
- 当提供相应输入时，对 live DB 查询结果或快照中的 revision/publication/evidence/instance/host/pool/workspace 账本执行检查；快照模式不查询数据库。

历史样例正例输出 `RESULT PASS_WITH_WARNINGS`（optional 非通过、fixture source），负例把定义/证据改成 `local-test-capacity-1` 后输出 `RESULT FAIL`、退出码 `1`，并给出 `definition.capacity_manifest_id`、`definition.placeholder`、`database.evidence_identity`、`database.evidence_placeholder` 等失败项。原始输出见 `docs/archive/operations/release-evidence-verification-output.txt`；重跑时会按当前时间检查有效期，不能预设结果相同。

脚本正常输出检查结果时，出现 `FAIL` 返回退出码 `1`；`PASS_WITH_WARNINGS` 和仅有 `NA` 的结果仍返回 `0`。因此必须检查各条 `WARN` / `NA` 的含义，退出码为零不证明未执行或无法判定的检查已通过。

当前随仓 `deploy/engines/file-asr-audio/binding-definition.json` 不是可用发布定义：校验器会直接报
`binding_definition.capacity_manifest_id=FAIL`（missing）和
`binding_definition.release_tier=FAIL`（`production` vs manifest 的 `experimental`），随后仍报
`digest.config_bundle=FAIL` 与实例 `config_digest=FAIL`。这是定义与证据不一致的事实，不能通过修改
fixture 或放宽校验消除；应由绑定作者发布新的不可变 revision。

## 11. 已知缺口与后续卡建议

1. **无机器可读阈值**：`Metric` 只有 name/value/unit/percentile，没有 threshold；阈值在 notes 里。建议后续 schema 演进增加 `threshold`/`comparison`，本任务不改冻结契约。
2. **报告不记录 license**：license 在 artifact pin；发布检查清单/法务接受需单独留痕。不要往报告塞自由字段。
3. **报告不记录模型文件逐个 digest**：只在 artifact pin；reviewer 必须把 pin 与制品存储核对。本任务未下载模型，不能声称已核验权重字节。
4. **内部工作与辅助阶段的覆盖**：当前样例只有 ASR stage；内部 VAD 的制品、资源和资格证据仍须覆盖。独立执行的对齐或分离阶段须在配方与 manifest 中声明并增加对应场景；报告不能只靠 `parameter_defaults` 推断。
5. **通用 binding 发布入口不验证报告原文件**：`speechnexus-publish-binding` 记录证据 ID 和可选摘要，并核对容量 manifest ID 与 binding 定义；它不读取报告文件核验完整性或摘要。离线脚本提供额外核对，不能单独当作产品门禁。内建声音资格的授权、签发和发布入口另行调用上述只读 release 核验组件，仍须遵守各自的部署和资格边界。
6. **`local-test-capacity-1` 禁止进入真实路径**：测试可以用，但真实 manifest/report/definition/evidence 的 id 必须是稳定、可追溯的真实 ID；脚本会拒绝 `local-test-*`。
