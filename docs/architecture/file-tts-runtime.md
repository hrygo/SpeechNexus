---
title: "文件 TTS 默认执行装配"
status: active
version: "0.1.0"
date: 2026-10-26
---

# 文件 TTS 默认执行装配

网关正式入口可按私有部署配置装配已有文件 worker、原 TTS 命令消费者、固定代次
vLLM-Omni、本地交付编码与私有结果适配器。未提供配置时保持原行为；这项接线不创建
binding、公开路由、音色发布、Job、预算或另一份结果账本。

真实请求携带 `max_output_audio_ms`、`sample_rate_hz`，并由正式入口派生
`output_stream_id`。装配后的 worker 先登记完整生成区间 `[0, source_pcm_samples)`，
再通过原观察事务登记音频回执；缺少覆盖、越过冻结上界或对象读回失败都会保留原占用。
组件接线不证明真实模型质量、公开消费、可信停止或物理集群资格。

固定制品、现有注册输入、显式默认及额外部署边界见
[CustomVoice 制品与参数交接](file-tts-binding-handoff.md)。

## 启用条件与责任

在[执行网关](execution-gateway.md)的组件凭据、显式 node/gateway、宿主锁目录、
已迁移数据库和运行角色就绪后，成组设置：

| 环境变量 | 内容 |
| --- | --- |
| `SPEECHNEXUS_GATEWAY_FILE_TTS_CONFIG_FILE` | 绝对路径，指向下述私有 JSON |
| `SPEECHNEXUS_GATEWAY_FILE_TTS_OBJECT_ACCESS_KEY` | 部署提供的对象存储访问身份 |
| `SPEECHNEXUS_GATEWAY_FILE_TTS_OBJECT_SECRET_KEY` | 对应密钥，仅从服务端设置读取 |

三项全部缺失保持未启用；部分提供、缺控制配置或相对路径拒绝启动。对象凭据不写入
JSON。ASR 与 TTS 可同时配置，共用一个持久命令 worker；两者的
`max_command_bytes`、`max_pending`、`shutdown_seconds` 必须一致，结果字节预算可分别设置。
配置不能与测试注入的工厂或控制服务混用。

组合根为[文件语音装配](../../execution-gateway/src/speechnexus_gateway/file_speech_runtime.py)、
[TTS 配置](../../execution-gateway/src/speechnexus_gateway/file_tts_runtime.py)及
[TTS 命令消费者](../../execution-gateway/src/speechnexus_gateway/interfaces/file_tts_command.py)。
interfaces 与 infrastructure 仍不互相导入。

## 私有配置格式

所有层级拒绝未知字段、重复键、非有限数字和隐式类型转换；配置上限为 65,536 bytes。
JSON 与自定义 CA 必须是当前运行 UID 所有、无 group/other 权限的非空普通文件，不接受
最终路径为 symlink、FIFO 或目录。

| 字段 | 含义 |
| --- | --- |
| `protocol` | 固定 `file_tts_v1` |
| `controlled_endpoint_file` | 受控私有描述绝对路径，沿用 `vllm_api_key_v1`、私网 IP 与 64 位小写十六进制生成凭据；目标池固定 `tts-batch` |
| `objects` | `endpoint`、`result_bucket`、`tls_ca_file`、`result_timeout_seconds`；固定私有 origin、开启版本化的结果桶与累计读回期限 |
| `tts_policy` | 完整原生 `TtsPolicy` 字段，包含 binding 的 `config_revision`、preset `voice`、`task`、`engine_format`、`sample_rate_hz`、`max_pcm_bytes`、`max_audio_ms`、`timeout_seconds` |
| `worker_limits` | 完整原生 `FileExecutionLimits` 字段；`max_result_bytes` 同时作为单次交付载荷上限 |

首批装配只接受 `task=CustomVoice`、24,000 Hz、`engine_format=wav|pcm`，交付格式仍由
命令的 `response_format` 决定。其它采样率、Base、VoiceDesign、流式文本和音色素材不属于
本单元。`tts_policy` 中即使有原生默认值也必须显式给出全部字段。

受限控制器可使用既有 `engine_auth=vllm_api_key_v1` 模板，通过
`/v1/audio/speech` 的固定、有界 `http_warmup_ok` 注入并验证代次凭据；
其派生作用域和容器漂移检查沿用[原生鉴权与代次关联](controlled-asr-generation.md)。
只通过 `/health` 不构成该模式的 ready，模板不得在 argv 覆盖原生 key。
部署须先升级控制器、核验固定镜像及实际预热，再提供网关描述；此配置不签发权威
binding、readiness 资格或准入预算。

## 激活与执行

1. `create_app` 有界读取配置并在数据库池、入口 ownership 与外部请求前完成严格校验；
   endpoint 的 node/gateway 必须匹配进程设置。错误不回显路径、凭据或文件正文。
2. lifespan 取得既有控制资源后构造 HTTP 适配器和 worker；激活后不重新读取可变配置。
3. 短 dispatch 只冻结命令，确认提交并退出 ingress 后才执行一次合成。请求必须带有
   冻结的 `max_output_audio_ms`、24 kHz `sample_rate_hz` 和派生 `output_stream_id`，
   其 binding/policy/voice 版本与目标 generation 必须匹配。
4. 合成结果先做结构校验与有界编码，再写私有 staging 并固定版本读回。worker 先登记完整
   生成区间，再用原 CAS 事务登记音频回执。重放查询原结果；重启隔离遗留活动执行，
   不把旧 intent 当重试队列。
5. 关闭先 join 文件工作，再关闭恢复驱动、数据库池与 ownership。HTTP 返回、本地线程退出
   和对象写成功都不签发停止或资源释放证明；未知执行继续占用，只由 A 凭匹配双证据结算。

## 尚未覆盖

固定版本的真实 CustomVoice 已完成[默认工厂和三格式私有交付实验](../audits/2026-10-26-b03-default-real-tts-probe.md)，
其证据不自动覆盖后续代码或模型组合。公开 `/v1/audio/speech` 路由、公开下载与 A 的发布链、
真实语料质量、长文本切分、流式/音色素材、生产停止与资源释放、
GPU 混合负载和物理多节点仍需独立交付。
默认 Compose 不提供私有配置、模型或已批准实例。

[真实取消与整代次停止联验](../audits/2026-10-26-b03-real-tts-stop-settlement.md)
已在隔离 PG/MinIO 中证明取消保持占用、控制器双证据由默认网关登记、A 原租约事务只结算
一次；就绪/准入和 runtime stop 意图生产者仍为 fixture，实例 base 结算与单请求释放未验。

## 验证范围

- `tests/unit/gateway/test_file_tts_runtime.py`：配置边界、目标绑定、双 lane 限制与注入冲突。
- `tests/unit/gateway/test_controlled_tts_target.py`：私有描述、池与 generation 匹配。
- `tests/unit/gateway/test_file_tts_command.py`：音频身份、时钟/上界与参数子集。
- `tests/unit/gateway/test_file_tts_worker.py`：先区间后回执，以及越过上界时隔离且不登记回执。
- `tests/integration/test_gateway_tts_results.py`：隔离 MinIO 与协调库中的音频回执路径。

这些测试使用合成音频与替身合成器，不加载权重、不访问真实 GPU，也不证明公开链或模型资格。
