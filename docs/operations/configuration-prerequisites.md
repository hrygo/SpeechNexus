---
title: "配置前提扫描"
status: active
version: "0.2.0"
date: 2026-11-13
---

# 配置前提扫描

本文回答一个问题：**一个键配置了却不起作用时，进程会不会告诉操作者？**

## 范围与方法

扫描覆盖四个启动装配面的全部 settings 类：

- `backend/src/speechnexus_backend/settings.py` 的 `Settings`，以及 `app.py` 组合根、
  `*_runtime.py` 的 `open()` 前置；
- `backend/src/speechnexus_backend/workbench/runtime_settings.py` 的 `WorkbenchSettings`；
- `execution-gateway/src/speechnexus_gateway/settings.py` 的 `Settings`；
- `node-controller/src/speechnexus_node_controller/settings.py` 的 `Settings`，以及
  `deploy/compose.yaml` 的 profile / 启动闸门前提。

清单由守护按 `SettingsConfigDict` 动态发现 settings 类，不靠类名约定；新增一个
settings 类或一个可选选择键都会让守护变红，直到在本表分类。

方法固定为「只配置被测键」：基线配置之外只加该键（或按表格成对配置），观察启动是否
拒绝、拒绝消息是否点名键名、以及拒绝之外的键是否真的改变行为。已知样本（#660）：
只配置 `SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL` 时进程无启动报错，发布仍返回
`503 voice_draft_unavailable`，与两个键全空完全一致；原因是 publisher 池由
`JobRuntime.open()` 创建，而 `JobRuntime` 只在
`SPEECHNEXUS_JOB_WRITER_DATABASE_URL` 非空时构造。

`当前表现` 取值：

| 取值 | 含义 |
| --- | --- |
| 点名拒绝 | 启动失败，消息含对应的消息点名内容（键名或身份概念）与该怎么做 |
| 静默无效 | 配了也不生效且不报错；本表不允许存在未处置的此类行 |
| 文档已声明 | 依赖关系已在操作文档中声明，操作者按文档可自行判断 |
| 启动告警点名 | 进程照常启动，但日志列出全部缺失键 |

「点名拒绝」有两类消息：`消息点名=键名` 表示消息里出现环境变量名（卡内新修项都要求
两个键名）；`消息点名=身份（不含键名）` 是既有实现只点名身份概念（例如
`Configure both object-storage keys together`），本卡没有改写这些既有消息。带条件的
行在「当前表现」里写明了条件，先读条件再读结论。

## 扫描表

| 键 | 生效前置 | 当前表现 | 消息点名 | 处置 |
| --- | --- | --- | --- | --- |
| `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL` | SPEECHNEXUS_JOB_WRITER_DATABASE_URL（admission 池由 JobRuntime.open() 创建） | 点名拒绝 | 键名 | 已修 #660 |
| `SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL` | SPEECHNEXUS_JOB_WRITER_DATABASE_URL（publisher 池由 JobRuntime.open() 创建） | 点名拒绝 | 键名 | 已修 #660 |
| `SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL`、`SPEECHNEXUS_JOB_WRITER_DATABASE_URL` | 同一个 database（host / port / database 一致） | 点名拒绝 | 键名 | 已修 #660 |
| `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL`、`SPEECHNEXUS_JOB_WRITER_DATABASE_URL` | 同一个 database（host / port / database 一致） | 点名拒绝 | 键名 | 已修 #660 |
| `SPEECHNEXUS_JOB_WRITER_DATABASE_URL`、`SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL`、`SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL`、`SPEECHNEXUS_RESOURCE_POOL_READER_DATABASE_URL`、`SPEECHNEXUS_BINDING_LIST_READER_DATABASE_URL`、`SPEECHNEXUS_AUDIT_READER_DATABASE_URL`、`SPEECHNEXUS_MODEL_INSTANCE_READER_DATABASE_URL`、`SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL`、`SPEECHNEXUS_PRONUNCIATION_READER_DATABASE_URL`、`SPEECHNEXUS_PRONUNCIATION_WRITER_DATABASE_URL`、`SPEECHNEXUS_PRONUNCIATION_PUBLISHER_DATABASE_URL`、`SPEECHNEXUS_BINDING_WRITER_DATABASE_URL`、`SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL`、`SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL`、`SPEECHNEXUS_BINDING_LIFECYCLE_DATABASE_URL`、`SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL` | SPEECHNEXUS_PROCESS_ROLE=api（每个运行体构造期校验） | 点名拒绝 | 身份（不含键名） | 无需：既有拒绝（按身份名拒绝，本卡不改写既有消息） |
| `SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL`、`SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE` | 两者互为前提（writer 必须有已评审证据文档，反之亦然） | 点名拒绝 | 键名 | 无需：既有拒绝（消息点名两个键） |
| `SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY`、`SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY` | 两者必须成对出现 | 点名拒绝 | 身份（不含键名） | 无需：既有拒绝（概念级消息，只点名身份） |
| `SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL`、`SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL` | 两者必须成对出现 | 点名拒绝 | 身份（不含键名） | 无需：既有拒绝（概念级消息，只点名身份） |
| `SPEECHNEXUS_BINDING_WRITER_DATABASE_URL`、`SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL` | 两种 binding 写入模式只能选一种 | 点名拒绝 | 身份（不含键名） | 无需：既有拒绝（概念级消息，只点名身份） |
| `SPEECHNEXUS_ASSET_INSPECTION_URL`、`SPEECHNEXUS_ASSET_INSPECTION_TOKEN` | SPEECHNEXUS_ASSET_INSPECTOR=audio 且资产运行时已装配（SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY/SECRET_KEY 非空） | 点名拒绝（仅资产运行时已装配时；未装配时整块检查面关闭，见 §已知非拒绝形态） | 键名 | 无需：既有拒绝（消息点名键） |
| `SPEECHNEXUS_AUDIO_INSPECTION_MAX_DURATION_MS`、`SPEECHNEXUS_AUDIO_INSPECTION_MAX_REFERENCE_DURATION_MS`、`SPEECHNEXUS_AUDIO_INSPECTION_MAX_INPUT_BYTES`、`SPEECHNEXUS_AUDIO_INSPECTION_WALL_SECONDS`、`SPEECHNEXUS_AUDIO_INSPECTION_MAX_RECEIPT_BYTES` | SPEECHNEXUS_ASSET_INSPECTOR=audio 且资产运行时已装配（文本模式下无消费者） | 点名拒绝（仅资产运行时已装配时；未装配时整块检查面关闭，见 §已知非拒绝形态） | 键名 | 已修 #660 |
| `SPEECHNEXUS_AUDIO_INSPECTION_MEMORY_BYTES`、`SPEECHNEXUS_AUDIO_INSPECTION_CPU_SECONDS`、`SPEECHNEXUS_AUDIO_INSPECTION_OUTPUT_BYTES` | 无（解码预算由检查器容器拥有，本进程不再消费） | 点名拒绝 | 键名 | 无需：既有拒绝（消息点名键与检查器侧替代键） |
| `SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT`、`SPEECHNEXUS_AUDIO_INSPECTION_COMMAND`、`SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON`、`SPEECHNEXUS_AUDIO_INSPECTION_BUBBLEWRAP`、`SPEECHNEXUS_AUDIO_INSPECTION_PRLIMIT` | 无（容器拥有这些输入，本进程不再消费） | 点名拒绝 | 键名 | 无需：既有拒绝（消息点名键与替代键） |
| `SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL` | SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY/SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY（资产运行时装配） | 文档已声明 | — | 无需：既有文档声明（无对象存储凭据时业务端点 503） |
| `SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL` | 资产运行时已装配（成对的 SPEECHNEXUS_OBJECT_STORAGE_* 凭据） | 文档已声明 | — | 无需：既有文档声明（只设置本变量不会装配写端口） |
| `SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE`、`SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET`、`SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL`、`SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL`、`SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY`、`SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY` | 整套同步转写输入（缺任一项即不装配） | 启动告警点名 | — | 无需：既有启动告警点名全部缺失键 |
| `SPEECHNEXUS_SPEECH_RECIPE_FILE`、`SPEECHNEXUS_SPEECH_DEFAULT_LANGUAGE`、`SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL`、`SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL`、`SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL`、`SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY`、`SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY` | 整套同步 TTS 输入（缺任一项即不装配） | 启动告警点名 | — | 无需：既有启动告警点名全部缺失键 |
| （数值与并发旋钮组） | 所属运行体已装配（对应 DSN/URL/凭据非空） | 文档已声明 | — | 无需：文档声明作用域（数值旋钮无法区分 Compose 透传默认值） |
| `SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL` | 启用 job-dispatch profile 并启动其 worker 入口 | 点名拒绝（仅 worker 入口；配在 API 进程上被 extra=ignore 静默忽略，见 §拼写与未知键） | 键名 | 无需：既有启动闸门点名拒绝 |
| `SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL` | SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN（执行控制启用） | 点名拒绝 | 键名 | 已修 #660 |
| `SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN`、`SPEECHNEXUS_GATEWAY_INGRESS_LOCK_DIRECTORY`、`SPEECHNEXUS_GATEWAY_RECOVERY_CONFIG_FILE`、`SPEECHNEXUS_GATEWAY_RECOVERY_NODE_COMPONENT_TOKEN`、`SPEECHNEXUS_GATEWAY_FILE_ASR_CONFIG_FILE`、`SPEECHNEXUS_GATEWAY_FILE_ASR_OBJECT_ACCESS_KEY`、`SPEECHNEXUS_GATEWAY_FILE_ASR_OBJECT_SECRET_KEY`、`SPEECHNEXUS_GATEWAY_FILE_TTS_CONFIG_FILE`、`SPEECHNEXUS_GATEWAY_FILE_TTS_OBJECT_ACCESS_KEY`、`SPEECHNEXUS_GATEWAY_FILE_TTS_OBJECT_SECRET_KEY`、`SPEECHNEXUS_GATEWAY_FILE_VOICE_DESIGN_CONFIG_FILE`、`SPEECHNEXUS_GATEWAY_FILE_VOICE_DESIGN_OBJECT_ACCESS_KEY`、`SPEECHNEXUS_GATEWAY_FILE_VOICE_DESIGN_OBJECT_SECRET_KEY`、`SPEECHNEXUS_GATEWAY_FILE_VOICE_CLONE_CONFIG_FILE`、`SPEECHNEXUS_GATEWAY_FILE_VOICE_CLONE_OBJECT_ACCESS_KEY`、`SPEECHNEXUS_GATEWAY_FILE_VOICE_CLONE_OBJECT_SECRET_KEY` | 执行控制启用、配置为绝对路径且每类引擎成对配置 | 点名拒绝 | 身份（不含键名） | 无需：既有拒绝（概念级消息，只点名身份） |
| `SPEECHNEXUS_CONTROLLER_ENGINE_AUTH_ROOT_KEY` | 至少一个获批模板声明 engine_auth（否则凭据永不被读取） | 点名拒绝 | 键名 | 已修 #660 |
| `SPEECHNEXUS_WORKBENCH_LLM_ENDPOINT`、`SPEECHNEXUS_WORKBENCH_LLM_MODEL`、`SPEECHNEXUS_WORKBENCH_LLM_API_KEY`、`SPEECHNEXUS_WORKBENCH_LLM_QUALIFICATION_REFERENCE` | 四个键同配；缺任一项即不装配内网 LLM | 点名拒绝 | 键名 | 已修 #660（LLM_API_KEY 单独配置此前被静默忽略） |
| `SPEECHNEXUS_WORKBENCH_SPEECH_BASE_URL`、`SPEECHNEXUS_WORKBENCH_OBJECT_ENDPOINT`、`SPEECHNEXUS_WORKBENCH_OBJECT_BUCKET`、`SPEECHNEXUS_WORKBENCH_OBJECT_ACCESS_KEY`、`SPEECHNEXUS_WORKBENCH_OBJECT_SECRET_KEY` | 五个键同配；缺任一项即拒绝而不是静默关闭保存 | 点名拒绝 | 身份（不含键名） | 无需：既有拒绝（概念级消息，只点名身份） |

## 数值与并发旋钮

`*_SECONDS`、`*_CONCURRENCY`、`*_MAX_*` 这类数值与并发旋钮只有在所属运行体已装配时
才生效；对应 DSN/URL/凭据为空时它们没有消费者。本表不把这种情况升级为启动拒绝：
`deploy/compose.yaml` 以 `${VAR:-默认值}` 透传这些键，进程无法区分「操作者显式写了默认值」
与「Compose 替未设置变量转发默认值」，因此按值判定实现不了可靠的点名。

例外是「解码预算迁移」三个键（`SPEECHNEXUS_AUDIO_INSPECTION_MEMORY_BYTES`/`_CPU_SECONDS`/
`_OUTPUT_BYTES`）：它们不是按值静默，而是只要显式改成非默认值就点名拒绝并给出检查器侧
替代键，已在扫描表单列。五个 API 侧音频预算的默认值在 `settings.py`、`asset_runtime.py`、
`deploy/compose.yaml` 与 `deploy/compose.env.example` 之间由守护逐值绑定，改动任一处
都会变红。

## 已知非拒绝形态

以下形态本卡**没有**升级为拒绝，如实登记，避免按表误判：

- **资产运行时未装配时，资产检查器整块静默**：`create_app` 只在对象存储凭据
  （`SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY` 与 `_SECRET_KEY`）都存在时构造
  `AssetRuntime`。没有凭据时 `SPEECHNEXUS_ASSET_INSPECTOR=audio`、检查器 URL/TOKEN 与
  五个音频预算都被接受且无消费者，进程正常启动；业务端点按
  [生产资产说明](../components/production-assets.md) 返回既有的 503。这是既有的
  「资产运行时未装配」语义，扫描表对应行的「当前表现」已写明条件。
- **`SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL` 配在 API 进程上**：该键只被
  `job-dispatch` worker 入口（及其启动闸门）读取；API 进程的 settings 有该字段以外的
  组合根没有消费者，配在 API 上不会生效也不会报错。表行已写明「仅 worker 入口」。
- **拼错或未声明的键**：见下一节。

## 拼写与未知键

四个服务的 settings 都以 `extra="ignore"` 加载环境：只匹配各自前缀、且必须在模型里
声明过的键才会被读取。因此把 `…_DATABASE_URLS`、`…_DATABSE_URL` 这类拼写错误的键写进
env 文件或平台 env 列表时，它既不会让启动失败，也不会出现在本扫描表的清单里——扫描
表只覆盖**已声明字段**。判断办法是先确认键名与 `deploy/compose.env.example` 或对应
settings 类完全一致，再用 `docker compose config` 核对实际注入；不要以「进程起来了」
当作键已生效的证据。本卡未改变 `extra` 语义（改成 `forbid` 会让平台注入的其他键变成
新的启动拒绝），这一形态登记为已知未覆盖面。

## 元守护

`tests/ci/test_configuration_prerequisites.py` 是本表的守护，做六件事：

1. **清单完整性**：按 `SettingsConfigDict` 动态发现四个模块里的全部 settings 类，
   类名与环境前缀与 `SETTINGS_CLASSES` 逐一绑定；每个可选选择键
   （`SecretStr`/`Path`/`AnyHttpUrl`/`str` 且可为 `None`）必须在本表有一行，或带理由
   的 `EXEMPT`。新加 settings 类、改前缀、或新加一个未分类的键都会变红。
2. **判定存在性**：每个「点名拒绝 / 启动告警点名」行都带 `probes`，指向产品源码里的
   函数/方法/常量；守护用 `ast` 定位其源码区间，并要求该行 `message_keys` 的每个键名
   出现在区间里。删掉判定、清空判定、或把消息换成通用句都会变红——**不需要跑被引用
   用例**。
3. **证据存在**：每行的 `evidence` 指向具体测试函数或文档句子；改名或改文案都会变红。
4. **静默行处置**：`静默无效` 行只能写「已开 issue #N」；既宣称已修又保持静默是自相
   矛盾，直接变红。
5. **默认值一致性**：音频预算在 settings、runtime 元组、Compose 与环境示例之间的默认
   值逐值绑定。
6. **文档同步**：本表与模块的键、当前表现、消息点名、处置逐格一致；本文承诺的小节
   （已知非拒绝形态、拼写与未知键、本段）缺失也会变红。

它**只证明判定存在，不证明行为**：第 2 条能证明源码里仍有带键名的判定，但「被引用
用例是否真在断言点名」「判定是否在所有部署路径上可达」由被引用测试与审查覆盖。它抓
不住：从未在 settings 类里声明过的隐式配对（`extra="ignore"` 下拼错键名根本不成字段）、
数值旋钮的作用域、平台在启动命令里注入的键，以及 `deploy/compose.yaml` 之外编排器
自行注入的键。

## 未覆盖

- 上述「已知非拒绝形态」三类。
- `tests/ci` 之外的判定行为：本守护在第 2 条之外不执行单测；单测是否仍在 CI 中执行由
  CI 路由决定。
- 未列入扫描的四类 worker 入口（`binding-disable`、`voice-evidence` 等）只做了入口级
  抽查，未逐键复跑。
