---
title: "文件 CustomVoice 固定制品与参数交接"
status: active
version: "0.1.1"
date: 2026-10-26
---

# 文件 CustomVoice 固定制品与参数交接

本交接供 B00 #27、B03 #30/#142 与 A #21/#22/#23 使用。它提供现有注册生产者能消费的
完整输入，不创建权威 binding、实例、manifest、资格、已发布声音或默认指针。
ASR 的独立交接见[文件 ASR](file-asr-binding-handoff.md)；无需等待整个 B03 关闭才接入。

## 固定制品与许可来源

[artifact-pin.json](../../deploy/engines/file-tts-customvoice/artifact-pin.json) 固定：

- 官方仓库 `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice` 的不可变 revision
  `0c0e3051f131929182e2c023b9537f8b1c68adfe`。
- 11 个运行制品，合计 **4,520,159,586 bytes**：主模型、文本 tokenizer/配置，以及
  `speech_tokenizer/` 下的 codec 权重和配置。每项复用既有 `ArtifactReference`，
  `artifact_id` 按内容摘要派生，仍须由注册方核验引用。
- 固定模型卡 `README.md` 的 Git blob、长度及 SHA256，作为 Apache-2.0 声明来源；
  不声称该模型仓库另有独立 LICENSE 文件。
- 显式忽略根目录的 `.complete`、`.gitattributes`、`configuration.json`。
  `speech_tokenizer/configuration.json` 是固定上游制品，必须校验。

2026-10-26 核对官方固定 tree，无分页遗漏：两份 safetensors 核对 LFS SHA256 与长度，
其余运行文件和模型卡核对 Git blob 与长度，再记录本机 SHA256；12 个文件一致。
此前真实实验的 13 文件指纹包含模型卡和根 `configuration.json`，总计
4,520,217,479 bytes。两种计数口径不同，不能据总字节变化推断模型被替换。
核对未上传业务内容、读取私人语料或加载模型。

[runtime-pin.json](../../deploy/engines/file-tts-customvoice/runtime-pin.json) 固定：

- `vllm/vllm-omni@sha256:5a12eac1a2dd3540a6ec29e42ef0dd5286c75ee07f68e497cb5f0df6e1311a52`；
  本机镜像 ID 与该 RepoDigest 一致，不以浮动 tag 部署。
- vLLM-Omni `0.32.0rc1`、vLLM `0.32.0`、Python `3.12.3`，
  338 个安装分发包的规范 JSON 清单摘要。
- vLLM-Omni commit `da572cddb38383deae9caef33b8d490e292ef071`，
  关键 HTTP、模型、pipeline、部署配置和 CLI 源码摘要；所列文件均与官方该 commit 一致。
- 官方该 commit 的 Apache-2.0 LICENSE 摘要和长度。

镜像核验使用无网络、无 GPU、只读、无 capabilities 的一次性容器，容器已删除；
35B、embedding、reranker 的容器身份、运行状态和启动时间未变。
这不等于全部依赖和编解码制品已完成再分发许可审核。
完整依赖清单、上游核对及原件保留在忽略目录 `.runtime/probes/customvoice-handoff/`。

## 离线命令与兼容边界

复用[现有工具](../../scripts/dev-env/verify-file-asr-artifacts.py)，保留历史文件名、
v1 ASR pin、原参数及原输出。v2 支持最多四层具名路径和无 VAD 的 vLLM-Omni；
没有复制校验器或创建第二套绑定协议。

```bash
mkdir -p .runtime/verification
uv run python scripts/dev-env/verify-file-asr-artifacts.py \
  --pin deploy/engines/file-tts-customvoice/artifact-pin.json \
  --model-root "$SPEECHNEXUS_TTS_MODEL_ROOT" \
  > .runtime/verification/file-tts-artifacts.json
```

工具复核仓库 runtime pin 和模型，输出原 `ArtifactReference`、`EngineDescriptor`、
revision、pin 摘要和总字节数，不输出本机路径。拒绝重复键、非有限/浮点 pin 数值、
重复文件/标识、路径越界、文件/目录冲突、额外条目、符号链接、FIFO 及长度/摘要变化。
只遍历清单要求的目录，读取有界；绑定定义允许契约合法的有限小数，不截断速度值。
缺少 v1 VAD、给 v2 添加 VAD 或缺少注册身份，均以脱敏 usage error 拒绝。

pin 是可信输入；自选 `--pin` 不证明上游真实性。离线校验不连接 Docker、引擎、
数据库或网络，不重新证明运行镜像、源码或依赖，也不签发 generation、ready 或停止事实。
部署必须只读挂载相同制品，并核对实际镜像/argv/环境及代次，防止检查后变化。

## 完整草稿注册输入

[binding-definition.json](../../deploy/engines/file-tts-customvoice/binding-definition.json)
固定参数声明与显式默认，身份和 manifest 引用留给注册方：

```bash
uv run python scripts/dev-env/verify-file-asr-artifacts.py \
  --pin deploy/engines/file-tts-customvoice/artifact-pin.json \
  --model-root "$SPEECHNEXUS_TTS_MODEL_ROOT" \
  --emit-binding-request \
  --binding-definition deploy/engines/file-tts-customvoice/binding-definition.json \
  --binding-id "$SPEECHNEXUS_TTS_BINDING_ID" \
  --logical-model "$SPEECHNEXUS_TTS_LOGICAL_MODEL" \
  --capacity-manifest-id "$SPEECHNEXUS_TTS_CAPACITY_MANIFEST_ID" \
  > .runtime/verification/file-tts-binding-request.json
```

输出是精确的既有 `BindingDefaultsCreateRequest`，可交给
`POST /v1/nexus/admin/model-bindings`；没有报告 envelope、revision 或 snapshot 身份。
工具复用实际生产者的纯准备入口验证 schema 和默认/支持域关系，不发送请求。
注册时保存原幂等键和原请求，重放不重建默认；固定默认通过既有 binding revision 的
`parameter-defaults` GET 读取。

`capacity_manifest_id` 必须由注册方提供有效引用，工具不证明其存在或合格。
`release_tier=production` 是申请目标；草稿响应的发布资格引用仍为空。
A 负责权威注册、资格校验、发布/激活与默认选择，B 提供固定制品、执行参数和可信事实。

| 项目 | 本候选声明与默认 |
| --- | --- |
| 任务与配方 | `tts_file`、单一 `tts_connector`、完整预留；不增加独立 codec 调度权威 |
| 语言 | `zh` / `en`，默认 `zh`；vendor 边界转换为 `Chinese` / `English` |
| 文本/交付 | `explicit` / `complete`，一次 intent 一次合成，最多 400 codepoints |
| planner | 显式 `max_chars=400`、`max_wait_ms=1000`；不实现等待窗口或长文本切分 |
| 交付格式 | `mp3` / `wav` / `pcm`，默认 MP3；24,000 Hz mono PCM16 |
| 速度 | 公共交集 0.5–2.0，默认 1.0；原生领域更宽范围不扩大本声明 |
| 风格/seed | 默认 `instructions=null`、`seed=null`；支持显式指令和公共非负 31 位 seed，不承诺位级复现 |
| 发音资源 | `pronunciation_revision=null`，无词表、发音资源或声音参考材料 |
| 保存 | `save_artifacts=true` 是命令默认，不代表 staging 已公开发布 |

声明与确定性测试只证明接收和映射，不把全部速度、语言、风格、seed、400 字符组合
声称为真实质量或容量资格。原默认工厂的三格式及停止实验仍分别绑定原受检版本。

## 部署还必须固定的事实

默认快照不包含所有部署参数，注册请求不能替代以下配置：

| 边界 | 需要明确的值或关联 |
| --- | --- |
| preset 声音 | 首条链使用 `CustomVoice` / `Vivian`；A 提供匹配的 builtin voice revision，B 策略冻结 vendor speaker；binding 草稿不创建声音资源 |
| 引擎格式 | `wav` 或 `pcm`、24,000 Hz，与冻结身份和响应头一致；公开 MP3 由本地编码生成 |
| 策略身份 | binding/policy revision、node/gateway/pool/instance/generation 与受控 endpoint 必须一致，不用示例身份补缺 |
| 输入/输出 | 命令长度、`max_pcm_bytes`、`max_audio_ms`、HTTP 总期限、交付字节预算、worker 容量和关闭期限全部显式提供；见[默认装配](file-tts-runtime.md) |
| 硬输出上界 | 原身份 `max_output_audio_ms` 不超过策略，源时钟为 24 kHz；先完整区间，后音频回执 |
| 控制器 | 批准模板、固定镜像/argv/挂载、代次 key 和受保护 `/v1/audio/speech` 两次有界预热；health-only 不算 ready |
| 指令长度 | 固定版本默认 pipeline 声明 500 codepoints；Nexus overrides 上限 2000，标准 speech 请求及内部执行命令上限均为 4096。模板目标 2000 的实际 argv、请求校验消费者及各入口映射/准入语义均须验证，不能只加 `--tts-max-instructions-length 2000` 就认定已支持；不得静默截断 2001–4096。既有短指令实验不能证明上界已验收 |
| 生成预算 | 模型 `generation_config.json` 的 `max_new_tokens=8192` 不等于最终引擎上限；固定 `qwen3_tts.yaml` 的 talker `max_tokens=4096`、code2wav 65536，serving ceiling 和 pipeline constraints 仍生效 |

模型配置的温度 0.9、top-k 50、top-p 1.0、repetition penalty 1.05 及引擎阶段配置随
制品/源码固定。网关不暴露任意 TTS sampling overrides，不能假设客户端能改这些值，
也不能只据模型文件判断最终采样参数。改变部署配置或参数上界须形成新配置身份并重验。

指令长度分别来自 [TTS overrides](../../contracts/shared/speech-overrides.schema.json)、
[标准 speech 请求](../../contracts/speech-openapi.yaml)和
[内部执行命令](../../contracts/internal/execution-command.schema.json)。
截至本轮收尾，只确认 serving 计算/缓存长度配置；是否传到 adapter 的实际校验尚待核实。
没有变更公共契约或产品实现，也没有完成 2000/4096 边界真实合成与超限拒绝实验。
该缺口及旧排期停止执行的安排见[负责人交接](../audits/2026-10-26-team-b-handoff.md)。

manifest 仍须覆盖 GPU、CPU 编码、响应与结果缓冲等全部资源。旧实验的 16 GiB cgroup
上限及 `gpu-memory-utilization=0.06` 不授予隔离或峰值/稳态资格。

## 验证、依赖与下一出口

确定性验证使用合成制品，不加载模型：

- 旧 ASR 回归继续通过；新 TTS 回归覆盖嵌套目录、错误制品/runtime 元数据、脱敏 CLI、
  有限小数、原注册生产者及实际文件命令消费者。
- PG HTTP 交接测试消费 ASR/TTS 请求，验证创建、固定默认读取、原键重放、改参冲突与
  重新装配；既有存储回归另覆盖 PG/InnoDB。
- 真实离线复核覆盖 11 个运行制品；无 GPU 镜像检查核对版本、依赖与所列源码。

2026-10-26 本候选验证：ASR/TTS 制品及命令消费者共 **187 项通过**；
交接、默认 HTTP 和 PG/InnoDB 存储共 **75 项通过，无跳过**，临时 PG 确认停止。
保留既有 TestClient 弃用警告。Ruff format/check、mypy、7 条依赖边界、
32 份契约、Web 格式和本地链接检查通过；没有业务源码或公共协议变更。
真实离线报告 SHA256 为
`000cecc5176cc76452e3dcd505aaf69bd17f4ff0c614a855fc8e1e0c49810a53`；
其 pin 摘要固定本次核验输入，不授予运行资格。

这些证据不代表真实制品已注册、正式准入、公开路由、发布/下载或物理集群已通过。
接入顺序：A 核验注册/固定默认 → 正式资格/readiness/Job/完整准入 →
B 受控执行及私有回执/停止事实 → A 原子发布 → SDK/C 试听下载。
ASR 交接已经在主线，TTS 可独立交接；无需等待任何父包全部关闭。

取消/超时/EOF 不证明停止，unknown 继续占用；匹配停止与释放双证据后才由 A 原事务
幂等结算。单请求释放、真实故障恢复、持续 ready、长文本、Base/VoiceDesign/克隆、
质量/混合负载和物理多机仍分别待交付。
