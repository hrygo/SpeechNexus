---
title: "文件 ASR 默认执行装配"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 文件 ASR 默认执行装配

网关正式入口 `speechnexus-gateway` / `speechnexus_gateway.app:create_app` 可按私有部署配置装配
已有文件 worker、原命令消费者、固定代次 vLLM、CPU VAD、PyAV 与 MinIO 输入/结果适配器。
未提供配置时保持原行为：提交/中止没有引擎 adapter，不接受真实执行。
这项接线不创建 binding、默认参数、资格、Job、预算或另一份结果账本。

当前验证使用真实 PostgreSQL、MinIO、PyAV 和回环 HTTP，VAD 与识别输出为替身。
尚未以该默认入口加载真实模型，不授予可信停止/释放、质量、容量、公开 SDK/C 或物理集群资格。
默认 Compose 仍不提供这些私有配置、模型依赖或已批准实例。

## 启用条件与责任

先按[执行网关](execution-gateway.md)提供组件凭据、显式 node/gateway、宿主锁目录、
已迁移的数据库与网关运行角色，再成组设置：

| 环境变量 | 内容 |
| --- | --- |
| `SPEECHNEXUS_GATEWAY_FILE_ASR_CONFIG_FILE` | 绝对路径，指向下述私有 JSON |
| `SPEECHNEXUS_GATEWAY_FILE_ASR_OBJECT_ACCESS_KEY` | 部署提供的对象存储访问身份 |
| `SPEECHNEXUS_GATEWAY_FILE_ASR_OBJECT_SECRET_KEY` | 对应密钥，仅从服务端设置读取 |

三项全部缺失保持未启用；部分提供、缺控制配置或相对配置路径拒绝启动。
对象凭据不写入 JSON；使用只对批准输入和私有结果范围授权的身份。
文件执行配置不能与测试注入的工厂或控制/提交/中止服务混用。
可同时配置原有恢复驱动，两者复用原 ledger、进程入口和关闭顺序。

组合根为[配置与装配模块](../../execution-gateway/src/speechnexus_gateway/file_asr_runtime.py)。
它同时引用 interfaces 和 infrastructure，二者仍不互相导入。
生产部署另外安装[CPU VAD 固定依赖](../../deploy/engines/cpu-vad/requirements.txt)及
[媒体运行依赖](file-asr-audio.md)；网关普通导入不加载模型或 vendor runtime。
文件、模型与服务身份由部署提供，不从公开请求接受路径、URL 或凭据。

## 私有配置格式

此文件描述单个进程的装配，不是公开协议或权威模型注册接口。
字段与解析行为以组合根及其调用的原生类型为准，所有层级拒绝未知字段、重复键、
非有限数字及隐式类型转换。配置上限为 65,536 bytes。
JSON 与自定义 CA 必须是当前运行 UID 所有、无 group/other 权限的非空普通文件，
不接受最终路径为 symlink、FIFO 或目录；读取不会等待 FIFO。
CA 上限为 262,144 bytes。

顶层必须显式提供以下全部字段：

| 字段 | 含义 |
| --- | --- |
| `protocol` | 固定 `file_asr_v1` |
| `controlled_endpoint_file` | [受控 ASR 描述](controlled-asr-generation.md)的绝对私有路径；沿用其 `vllm_api_key_v1` 及凭据要求 |
| `vad_model_file` / `vad_model_sha256` | 已下载普通 ONNX 文件的绝对路径、64 位小写十六进制 SHA-256；允许模型卷软链接，打开后的目标须为普通文件，启动核对实际内容摘要和 CPU 模型接口 |
| `objects` | 下表的固定对象存储范围 |
| `vad_tuning` | 完整原生 `VadPolicy` 字段，包含固定策略修订 |
| `input_limits` | 完整原生 `ExecutionInputLimits` 字段 |
| `decode_limits` | 完整原生 `AudioDecodeLimits` 字段 |
| `worker_limits` | 完整原生 `FileExecutionLimits` 字段 |
| `max_completion_tokens` / `max_call_seconds` | 显式单段 token 与调用秒数上限，由原生 `AsrPolicy` 校验 |

`objects` 的全部字段如下：

| 字段 | 含义 |
| --- | --- |
| `endpoint` | 固定私有 IP origin；沿用对象适配器的协议、地址与验证 TLS 规则，不使用 DNS、代理或重定向 |
| `input_bucket` / `result_bucket` | 已批准的输入桶和开启版本化的私有结果桶；可以相同 |
| `spool_directory` | 已存在的绝对目录，当前 UID 所有且无 group/other 权限；仅作有界临时缓冲 |
| `tls_ca_file` | 显式为 `null`，或私有 CA 绝对路径；`null` 使用系统信任，不关闭证书验证 |
| `result_timeout_seconds` | 结果 PUT 与固定版本读回的累计秒数上限，复用结果适配器原生校验 |

原生对象也必须提供完整字段，即使字段在独立原生 API 中有默认值：

| 配置对象 | 必须显式给出的字段 | 校验来源 |
| --- | --- | --- |
| `vad_tuning` | `config_revision`、`threshold`、`end_threshold`、`min_speech_ms`、`prefix_padding_ms`、`silence_duration_ms`、`max_segment_ms` | [VadPolicy](../../execution-gateway/src/speechnexus_gateway/domain/vad.py) |
| `input_limits` | `max_bytes`、`memory_bytes`、`timeout_seconds` | [ExecutionInputLimits](../../execution-gateway/src/speechnexus_gateway/domain/execution_inputs.py) |
| `decode_limits` | `max_encoded_bytes`、`max_duration_ms`、`max_frame_samples`、`timeout_seconds` | [AudioDecodeLimits](../../execution-gateway/src/speechnexus_gateway/domain/media.py) |
| `worker_limits` | `max_result_bytes`、`max_command_bytes`、`max_pending`、`shutdown_seconds` | [FileExecutionLimits](../../execution-gateway/src/speechnexus_gateway/domain/file_execution.py) |

字节、毫秒、秒与样本数由字段名明确区分；范围直接复用这些类型，不维护第二份边界规则。
这些上限控制本地消费，不授予调度配额或模型容量。命令自身仍须携带已解析的参数、
资源与版本；[原命令消费者](frozen-execution-inputs.md)按既有支持子集校验和转换，
不支持的能力明确拒绝。

## 激活、执行与关闭

1. `create_app` 有界读取并严格解析配置，冻结原生策略/上限、凭据和 endpoint。
   endpoint 的 node/gateway 必须匹配进程设置。错误信息不回显路径、凭据或文件正文；
   配置结构、目标或本地消费上界错误在数据库池、入口 ownership、模型加载和外部请求前拒绝。
2. lifespan 取得既有控制资源后，构造 PyAV、按摘要固定的 CPU VAD 和既有 worker。
   模型文件变化、摘要错误、依赖缺失或模型接口不符使启动失败，沿原 lifecycle 关闭控制资源；
   模型使用非阻塞普通文件读取，不能通过替换成 FIFO 卡住启动。激活后不重新读取可变配置。
3. worker 复用原 intent：短 dispatch 只准备，接收确认提交并退出 ingress 后才开始长处理。
   执行目标仍逐次核对完整 node/gateway/pool/instance/generation/binding；
   真实 ledger 继续核验租约、屏障、资格和版本。私有配置不替代这些权威检查。
4. 输入固定版本核验后才进入解码/VAD/ASR，结果先写私有 staging、版本化读回，再由原事务登记。
   重放查询原结果；重启将遗留活动执行隔离，不将旧 intent 当重试队列。
5. 关闭先停止并 join 文件工作，再停恢复驱动、关闭数据库池与释放 ownership。
   重复取消仍等待正在构造/启动的对象返回后清理；迟到活线程没有退出就不报告资源已关闭。

配置启用后，运行中的文件 worker 参加 `/readyz`；它只证明本地执行线程正在服务。
该探针不检查真实模型的输出质量、远端实例状态、对象桶版本化或完整公开业务链。
HTTP/SSE 完成、客户端取消及本地线程退出都不产生 GPU 停止或资源释放证明。
未知执行继续占用，只由 A 凭匹配双证据在原事务结算预算。

## 验证范围

- [配置和默认 lifecycle](../../tests/unit/gateway/test_file_asr_runtime.py)：拒绝缺省/未知字段、
  重复键、特殊文件、错误目标与上界；验证缺配置行为、默认装配、加载失败及重复取消。
- [默认入口集成](../../tests/integration/test_gateway_default_file_asr.py)：未注入工厂，
  使用正式 PG 角色、真实 MinIO/PyAV 与回环原生 HTTP；有语音和无语音分别验证，
  重放/重启不增加外发或结果版本，预算和停止证据不被改变。
- 原 worker 的双库、竞争及真实 commit 确认丢失回归继续保护原事务边界。
  InnoDB 仅验证协调关键路径；默认生产配置仍使用 PostgreSQL。

测试使用合成媒体和脚本化模型，不加载权重、不下载制品、不使用私人录音。
执行命令及隔离前置见[验证指南](../agent-verification.md)与[集成测试说明](../../tests/integration/README.md)。
