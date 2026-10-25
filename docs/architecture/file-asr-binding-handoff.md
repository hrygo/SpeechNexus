---
title: "文件 ASR 固定制品与参数交接"
status: active
version: "0.2.0"
date: 2026-10-25
---

# 文件 ASR 固定制品与参数交接

本交接供 B00 #27、B03 #30/#142 与 A #21/#22/#23 接通首条文件 ASR。
按 [ADR 0002](../decisions/0002-speech-models-engines-and-loading.md)，文件识别固定
Qwen3-ASR-1.7B，CPU Silero VAD 门控后逐段识别。0.6B 的实时候选与
CustomVoice TTS 各自交接，不以本单元替代。

## 固定输入与离线复核

[artifact-pin.json](../../deploy/engines/file-asr-audio/artifact-pin.json) 是此候选唯一
制品清单：上游仓库及不可变 revision、10 个 ASR 运行文件、模型卡、一个 VAD 权重、
许可来源与独立依赖锁文件。`artifact` 直接使用现有 ModelBinding 的
`ArtifactReference`；模型卡作为许可来源单列，不伪装成运行权重。

2026-10-25 本机核验结果：

- ASR 运行文件及 README 与固定上游 revision 一致；权重核对 LFS SHA256/长度，
  其他文件核对上游 Git blob，再记录 SHA256。ModelScope 的 `.complete`、
  `.gitattributes`、`configuration.json` 仅为明确忽略的元数据。
- VAD 固定已有上游 commit 与 ONNX 摘要，MIT notice 保存在
  [SILERO_LICENSE](../../deploy/engines/cpu-vad/SILERO_LICENSE)。
  ASR 的 Apache-2.0 声明来自固定模型卡；没有声称本机另有上游 LICENSE 文件。
- [runtime-pin.json](../../deploy/engines/vllm-asr-lifecycle/src/speechnexus_vllm_asr/runtime-pin.json)
  固定 vLLM 0.28.0 镜像和 11 个相关源码摘要。只读、无网络、无 GPU 的临时容器
  核对了这些摘要、Python 3.12.3 及 268 个安装包；包清单的规范 JSON 摘要记录在 pin，
  未加载模型。容器已删除，受保护服务身份和启动时间前后相同。
- 固定镜像内包含 torch 2.13.0+cu130、torchaudio 2.11.0+cu130、
  transformers 5.15.1、triton 3.7.1、numpy 2.2.6。独立 CPU 媒体环境使用
  [锁文件](../../deploy/engines/file-asr-audio/requirements.txt)，包括 PyAV 19.0.2、
  HTTPX 0.28.1、numpy 2.4.6 和 ONNX Runtime 1.31.0，不混用两个环境的 NumPy。

这些记录是固定制品的事实输入，未完成整体镜像/编解码依赖的再分发许可审核。
原始元数据与完整依赖/许可摘要保留在忽略的本机证据目录，不包含在公开契约中。

在仓库根目录运行：

```bash
mkdir -p .runtime/verification
uv run python scripts/dev-env/verify-file-asr-artifacts.py \
  --model-root "$SPEECHNEXUS_ASR_MODEL_ROOT" \
  --vad-file "$SPEECHNEXUS_VAD_MODEL_FILE" \
  > .runtime/verification/file-asr-artifacts.json
```

输出包含可供既有生产者使用的 `artifact_set`、`engine`、上游 revision、总字节数与
pin 的 SHA256，不输出模型路径。该次实际离线复核覆盖 11 个运行制品，
合计 4,705,382,857 bytes；README 和仓库锁文件另行核验。
`artifact_id` 是内容派生标识，仍须由 A 核验注册引用，不表示已经发布。

工具仅做文件 I/O：拒绝重复 JSON 键、未知字段、重复文件/标识、越界路径、非普通文件、
符号链接、额外模型文件及长度/摘要变化；读取有界，FIFO 不会阻塞校验。
失败仅输出固定错误类别。默认 pin 是版本控制中的可信配置；
自选 `--pin` 不能签发部署事实，校验也不会重新联系上游或证明运行容器与清单一致。
后续部署仍须只读挂载相同制品并核对可信镜像、实例及 generation，避免检查后文件变化。

## 参数由谁固定

源协议是 [ModelBinding](../../contracts/model-binding.schema.json)、
[显式默认生产者](binding-defaults-producer.md)和
[完整有效命令](../../contracts/internal/execution-command.schema.json)，不新建配置权威。

A 通过现有 `ModelBindingCreateRequest` / `BindingDefaultsCreateRequest` 分配
binding revision 与默认快照；B 提供 `artifact_set`、`engine`、支持范围和部署参数。
容量 manifest、资格、发布/激活与默认选择必须有各自事实，不能为了凑完整请求
伪造一个已合格 manifest 或自行分配 `revision_id`。当前 draft/default API 不等于
已发布配置解析或可运行默认装配。

### 生成完整草稿注册请求

[binding-definition.json](../../deploy/engines/file-asr-audio/binding-definition.json)
固定本候选的 `asr_file` 支持域、执行配方和显式默认。
默认模式继续输出原离线核验报告；增加 `--emit-binding-request` 后，工具先完整核验制品，
再输出精确的 `BindingDefaultsCreateRequest`，可交给既有
`POST /v1/nexus/admin/model-bindings`，不附报告 envelope 或本机路径：

```bash
uv run python scripts/dev-env/verify-file-asr-artifacts.py \
  --model-root "$SPEECHNEXUS_ASR_MODEL_ROOT" \
  --vad-file "$SPEECHNEXUS_VAD_MODEL_FILE" \
  --emit-binding-request \
  --binding-id "$SPEECHNEXUS_ASR_BINDING_ID" \
  --logical-model "$SPEECHNEXUS_ASR_LOGICAL_MODEL" \
  --capacity-manifest-id "$SPEECHNEXUS_ASR_CAPACITY_MANIFEST_ID" \
  > .runtime/verification/file-asr-binding-request.json
```

后三项身份/引用必须由注册方明确提供；工具没有默认值，定义文件也不得预置它们。
`capacity_manifest_id` 是现行配方契约要求的引用，离线工具不证明其存在或资格；
不得填虚构值用于真实注册。注册方仍须核验完整资源范围、许可及发布条件。
`release_tier=production` 是申请的目标等级，不是发布回执。
工具不发 HTTP、不写数据库，也不分配 revision、snapshot 或 publication 身份；
请求的 schema 和跨字段语义复用实际生产者的纯准备入口。

配方只有现有 `asr_batch` 执行角色。CPU 解码和 VAD 是文件消费者内部工作，
没有虚构独立可调度的 VAD 路由；完整 manifest 仍须覆盖它们的 CPU、内存、缓冲及 GPU 阶段，
不能从单个 vLLM descriptor 推断配方已经具备容量资格。

本候选声明 `auto` / `zh` / `en`、完整分段和 prompt，拒绝 rollback、词级时间戳、
alignment 和 diarization。显式默认仅覆盖现行快照支持的字段；
prompt、temperature 仍从完整有效命令读取，token/字节/内存上限及 VAD tuning
仍由对应部署配置固定，不能认为该 JSON 已包含全部执行配置。
默认的段期限不授予吞吐或冷启动资格，辅助期限也不会启用关闭的辅助阶段。

注册时使用调用方持久保存的原幂等键和同一请求；重放不重建或改写默认参数。
草稿响应的 `publication.capacity_manifest_id` 和 `qualification_report_id` 仍为 null，
固定默认快照通过该 binding revision 的
`GET /v1/nexus/admin/model-bindings/{binding_id}/revisions/{revision}/parameter-defaults`
读取。后续发布和默认选择各自验收。

`CommandFileAsr` 不读取引擎隐式默认：从原持久命令取 language、prompt、temperature、
turn detection、分段和期限；部署必须显式提供 token/调用/输入/解码/结果上限及 VAD tuning。
现有 `AsrPolicy` 的领域缺省 1024 tokens / 60 s 不会自动替调用方补齐这些装配参数。
参数变更产生新绑定/配置版本，不能改写原命令或重放时读取新的默认值。

| 决策位置 | 当前实现要求 |
| --- | --- |
| 任务与阶段 | `kind=asr`、`task_kind=asr_file`、`stage_role=asr_batch` |
| 固定关联 | recognizer binding revision 与执行一致；VAD config revision 与 policy revision 一致；无 voice revision/output stream |
| 分段模式 | `server_vad`、`full_segment`、`rollback_tokens=0`；`max_segment_ms >= preview_interval_ms` |
| 辅助与词表 | alignment/diarization 为 `off`，vocabulary revision 为 null，timestamp granularities 为空 |
| 可复用资源 | voice/voice_material/voice_kind/vocabulary_source/pronunciation_source 为 null；vocabulary/pronunciation 为 [] |
| 输出 | 请求仅 `json` / `text`；B 私有 staging 是完整 JSON 转写，A 按公开格式发布/投影 |
| prompt / language / temperature | 原命令显式给出；prompt 的 null 转为空字符串，`auto` 不向 vendor 传 language；不将合法语言标签当作已测语言资格 |
| token / 单次期限 | 部署显式给出，分别受 `AsrPolicy` 1–4096、(0,300] s 约束；实际调用取部署上限与原命令 `final_deadline_ms / 1000` 的较小值 |

以上取自
[`CommandFileAsr._resolve`](../../execution-gateway/src/speechnexus_gateway/interfaces/file_asr_command.py)
及 [AsrPolicy](../../execution-gateway/src/speechnexus_gateway/domain/asr.py)。
拒绝的能力在对象 I/O 前失败，不允许降级执行。公共错误转换仍由既有入口负责，
本单元没有新增公开错误码。

`auxiliary_deadline_ms` 当前没有辅助阶段消费；`preview_interval_ms` 仅参与跨字段校验，
不产生实时预览。[文件消费者](../../execution-gateway/src/speechnexus_gateway/application/file_asr.py)
从每段闭合时刻计算 `final_deadline_ms`，等待时间占用该段剩余期限，
不在实际外发时重新开始计时；外层执行身份 deadline 同时约束整个执行。
因此输入允许一小时不表示一小时录音已通过吞吐或期限验收，需在真实公开链按场景取证。

## 上界与未资格项

| 边界 | 当前来源及范围 |
| --- | --- |
| 原对象输入 | [ExecutionInputLimits](../../execution-gateway/src/speechnexus_gateway/domain/execution_inputs.py)：部署显式指定 max bytes、内存缓冲和总期限；不能只采用解码器上限替代对象下载核验 |
| 媒体 | [有界媒体输入](file-asr-audio.md)：解码缺省 256 MiB / 1 h / 30 s，五种格式；合法命令仍须满足当前 8–192 kHz、1–8 声道子集 |
| 单段 | 16 kHz mono PCM16LE，最多 30 s；VAD tuning 独立冻结，内存/分段约束见 [CPU VAD](cpu-vad.md) |
| 引擎响应 | [原生识别接口](file-asr-recognition.md)：token 上限、finish reason/usage/身份校验、文本/事件/总响应字节限制；截断不发布 |
| 完整私有结果 | [结果接口](file-asr-results.md)及 [MAX_ASR_RESULT_BYTES](../../execution-gateway/src/speechnexus_gateway/domain/asr_results.py)：调用方显式提供上限，最多 4 MiB；不因逐段合法而允许无限聚合 |
| 全局资源与停止 | 仍由原完整准入、实例生命周期和停止/资源释放双证据保证；局部字节上限不授予进程/设备容量 |

本单元没有新增 binding、manifest、资格或发布记录，也没有重跑模型/私人音频。
持续 ready、可信端点与 instance/generation 对应、真实旧入口封锁、长文件与语言质量、
混合负载及物理集群仍待验证。取消/EOF/请求清理不是停止证据，unknown 保留占用，
只有 A 原事务凭匹配的停止与释放双证据退预算。

接入顺序：A 核验并注册固定制品/配置 → 按原 manifest/资格选择可服务实例 →
B 默认装配消费同一完整命令 → A 原子发布回执 → SDK/C 同版联验。
这些分别是具名依赖，不要求整个 B00 先关闭，也不允许跨团队直接写表绕过。

## 本次交接验证

2026-10-25 在 main `e959c43` 上重放并验证本候选；此前 `5164b6a` 组合的同组回归也通过：

- `tests/unit/test_file_asr_artifacts.py` 与
  `tests/unit/gateway/test_file_asr_command.py` 共 122 项通过、无跳过，覆盖身份必填、
  原核验输出兼容、禁止外部 I/O、错误定义及现有文件命令对声明默认的接收。
- `tests/integration/test_file_asr_binding_handoff.py`、
  `tests/integration/test_binding_defaults_http.py` 与
  `tests/integration/test_binding_defaults_storage.py` 共 74 项通过、无跳过。
  新交接用合成制品生成请求，经实际 PG HTTP producer 创建草稿、固定读、同键重放、
  改默认冲突和应用重启；既有存储回归覆盖 PG/InnoDB 与 TypeScript 快照消费者。
- 使用本项目一次性 Unix socket PG，结束后确认停止；没有加载模型或使用私人音频。
  这不证明真实制品已权威注册、已发布、单请求释放、完整公开链或物理集群资格。
