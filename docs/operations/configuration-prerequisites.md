---
title: "配置前提扫描：配置了就必须生效或点名"
status: active
version: "0.1.0"
date: 2026-11-13
---

# 配置前提扫描

本文回答一个问题：**一个键配置了却不起作用时，进程会不会告诉操作者？**

## 范围与方法

扫描覆盖四个启动装配面：

- `backend/src/speechnexus_backend/settings.py` 的全部可选选择键，以及 `app.py` 组合根、
  `*_runtime.py` 的 `open()` 前置；
- `backend/src/speechnexus_backend/workbench/runtime_settings.py` 的配对校验；
- `execution-gateway/src/speechnexus_gateway/settings.py` 的配对校验；
- `node-controller` 的引擎凭据，以及 `deploy/compose.yaml` 的 profile / 启动闸门前提。

方法固定为「只配置被测键」：基线配置之外只加该键，观察启动是否拒绝、拒绝消息是否点名
键名、以及拒绝之外的键是否真的改变行为。已知样本（#660）：只配置
`SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL` 时进程无启动报错，发布仍返回
`503 voice_draft_unavailable`，与两个键全空完全一致；原因是 publisher 池由
`JobRuntime.open()` 创建，而 `JobRuntime` 只在
`SPEECHNEXUS_JOB_WRITER_DATABASE_URL` 非空时构造。

`当前表现` 取值：

| 取值 | 含义 |
| --- | --- |
| 点名拒绝 | 启动失败，消息含键名与该怎么做（卡内新修的还要求含两个键名） |
| 静默无效 | 配了也不生效且不报错；本表不允许存在未处置的此类行 |
| 文档已声明 | 依赖关系已在操作文档中声明，操作者按文档可自行判断 |
| 启动告警点名 | 进程照常启动，但日志列出全部缺失键 |

## 扫描表

| 键 | 生效前置 | 当前表现 | 处置 |
| --- | --- | --- | --- |
| `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL` | SPEECHNEXUS_JOB_WRITER_DATABASE_URL（admission 池由 JobRuntime.open() 创建） | 点名拒绝 | 已修 #660 |
| `SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL` | SPEECHNEXUS_JOB_WRITER_DATABASE_URL（publisher 池由 JobRuntime.open() 创建） | 点名拒绝 | 已修 #660 |
| `SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL`、`SPEECHNEXUS_JOB_WRITER_DATABASE_URL` | 同一个 database（host / port / database 一致） | 点名拒绝 | 已修 #660 |
| `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL`、`SPEECHNEXUS_JOB_WRITER_DATABASE_URL` | 同一个 database（host / port / database 一致） | 点名拒绝 | 已修 #660 |
| `SPEECHNEXUS_JOB_WRITER_DATABASE_URL`、`SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL`、`SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL`、`SPEECHNEXUS_RESOURCE_POOL_READER_DATABASE_URL`、`SPEECHNEXUS_BINDING_LIST_READER_DATABASE_URL`、`SPEECHNEXUS_AUDIT_READER_DATABASE_URL`、`SPEECHNEXUS_MODEL_INSTANCE_READER_DATABASE_URL`、`SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL`、`SPEECHNEXUS_PRONUNCIATION_READER_DATABASE_URL`、`SPEECHNEXUS_PRONUNCIATION_WRITER_DATABASE_URL`、`SPEECHNEXUS_PRONUNCIATION_PUBLISHER_DATABASE_URL`、`SPEECHNEXUS_BINDING_WRITER_DATABASE_URL`、`SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL`、`SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL`、`SPEECHNEXUS_BINDING_LIFECYCLE_DATABASE_URL`、`SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL` | SPEECHNEXUS_PROCESS_ROLE=api（每个运行体构造期校验） | 点名拒绝 | 无需：既有拒绝（按身份名拒绝，本卡不改写既有消息） |
| `SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL`、`SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE` | 两者互为前提（writer 必须有已评审证据文档，反之亦然） | 点名拒绝 | 无需：既有拒绝（消息点名两个键） |
| `SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY`、`SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY` | 两者必须成对出现 | 点名拒绝 | 无需：既有拒绝（概念级消息，只点名身份） |
| `SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL`、`SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL` | 两者必须成对出现 | 点名拒绝 | 无需：既有拒绝（概念级消息，只点名身份） |
| `SPEECHNEXUS_BINDING_WRITER_DATABASE_URL`、`SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL` | 两种 binding 写入模式只能选一种 | 点名拒绝 | 无需：既有拒绝（概念级消息，只点名身份） |
| `SPEECHNEXUS_ASSET_INSPECTION_URL`、`SPEECHNEXUS_ASSET_INSPECTION_TOKEN` | SPEECHNEXUS_ASSET_INSPECTOR=audio 且资产运行时已装配（SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY/SECRET_KEY 非空） | 点名拒绝 | 无需：既有拒绝（消息点名键） |
| `SPEECHNEXUS_AUDIO_INSPECTION_MAX_DURATION_MS`、`SPEECHNEXUS_AUDIO_INSPECTION_MAX_REFERENCE_DURATION_MS`、`SPEECHNEXUS_AUDIO_INSPECTION_MAX_INPUT_BYTES`、`SPEECHNEXUS_AUDIO_INSPECTION_WALL_SECONDS`、`SPEECHNEXUS_AUDIO_INSPECTION_MAX_RECEIPT_BYTES` | SPEECHNEXUS_ASSET_INSPECTOR=audio 且资产运行时已装配（文本模式下无消费者） | 点名拒绝 | 已修 #660 |
| `SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT`、`SPEECHNEXUS_AUDIO_INSPECTION_COMMAND`、`SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON`、`SPEECHNEXUS_AUDIO_INSPECTION_BUBBLEWRAP`、`SPEECHNEXUS_AUDIO_INSPECTION_PRLIMIT` | 无（容器拥有这些输入，本进程不再消费） | 点名拒绝 | 无需：既有拒绝（消息点名键与替代键） |
| `SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL` | SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY/SECRET_KEY（资产运行时装配） | 文档已声明 | 无需：既有文档声明（无对象存储凭据时业务端点 503） |
| `SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL` | 资产运行时已装配（成对的 SPEECHNEXUS_OBJECT_STORAGE_* 凭据） | 文档已声明 | 无需：既有文档声明（只设置本变量不会装配写端口） |
| `SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE`、`SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET`、`SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL`、`SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL`、`SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY`、`SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY` | 整套同步转写输入（缺任一项即不装配） | 启动告警点名 | 无需：既有启动告警点名全部缺失键 |
| `SPEECHNEXUS_SPEECH_RECIPE_FILE`、`SPEECHNEXUS_SPEECH_DEFAULT_LANGUAGE`、`SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL`、`SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL`、`SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL`、`SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY`、`SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY` | 整套同步 TTS 输入（缺任一项即不装配） | 启动告警点名 | 无需：既有启动告警点名全部缺失键 |
| （数值与并发旋钮组） | 所属运行体已装配（对应 DSN/URL/凭据非空） | 文档已声明 | 无需：本卡补充文档声明（数值旋钮无法区分 Compose 透传默认值） |
| `SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL` | 启用 job-dispatch profile 并启动其入口 | 点名拒绝 | 无需：既有启动闸门点名拒绝 |
| `SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL` | SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN（执行控制启用） | 点名拒绝 | 已修 #660 |
| `SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN`、`SPEECHNEXUS_GATEWAY_INGRESS_LOCK_DIRECTORY`、`SPEECHNEXUS_GATEWAY_RECOVERY_CONFIG_FILE`、`SPEECHNEXUS_GATEWAY_RECOVERY_NODE_COMPONENT_TOKEN`、`SPEECHNEXUS_GATEWAY_FILE_ASR_CONFIG_FILE`、`SPEECHNEXUS_GATEWAY_FILE_ASR_OBJECT_ACCESS_KEY`、`SPEECHNEXUS_GATEWAY_FILE_ASR_OBJECT_SECRET_KEY`、`SPEECHNEXUS_GATEWAY_FILE_TTS_CONFIG_FILE`、`SPEECHNEXUS_GATEWAY_FILE_TTS_OBJECT_ACCESS_KEY`、`SPEECHNEXUS_GATEWAY_FILE_TTS_OBJECT_SECRET_KEY`、`SPEECHNEXUS_GATEWAY_FILE_VOICE_DESIGN_CONFIG_FILE`、`SPEECHNEXUS_GATEWAY_FILE_VOICE_DESIGN_OBJECT_ACCESS_KEY`、`SPEECHNEXUS_GATEWAY_FILE_VOICE_DESIGN_OBJECT_SECRET_KEY`、`SPEECHNEXUS_GATEWAY_FILE_VOICE_CLONE_CONFIG_FILE`、`SPEECHNEXUS_GATEWAY_FILE_VOICE_CLONE_OBJECT_ACCESS_KEY`、`SPEECHNEXUS_GATEWAY_FILE_VOICE_CLONE_OBJECT_SECRET_KEY` | 执行控制启用、配置为绝对路径且每类引擎成对配置 | 点名拒绝 | 无需：既有拒绝（概念级消息，只点名身份） |
| `SPEECHNEXUS_CONTROLLER_ENGINE_AUTH_ROOT_KEY` | 至少一个获批模板声明 engine_auth（否则凭据永不被读取） | 点名拒绝 | 已修 #660 |
| `SPEECHNEXUS_WORKBENCH_LLM_ENDPOINT`、`SPEECHNEXUS_WORKBENCH_LLM_MODEL`、`SPEECHNEXUS_WORKBENCH_LLM_API_KEY`、`SPEECHNEXUS_WORKBENCH_LLM_QUALIFICATION_REFERENCE` | 四个键同配；缺任一项即不装配内网 LLM | 点名拒绝 | 已修 #660（`LLM_API_KEY` 单独配置此前被静默忽略） |
| `SPEECHNEXUS_WORKBENCH_SPEECH_BASE_URL`、`SPEECHNEXUS_WORKBENCH_OBJECT_ENDPOINT`、`SPEECHNEXUS_WORKBENCH_OBJECT_BUCKET`、`SPEECHNEXUS_WORKBENCH_OBJECT_ACCESS_KEY`、`SPEECHNEXUS_WORKBENCH_OBJECT_SECRET_KEY` | 五个键同配；缺任一项即拒绝而不是静默关闭保存 | 点名拒绝 | 无需：既有拒绝（概念级消息，只点名身份） |
## 数值与并发旋钮

`*_SECONDS`、`*_CONCURRENCY`、`*_MAX_*` 这类数值与并发旋钮只有在所属运行体已装配时
才生效；对应 DSN/URL/凭据为空时它们没有消费者。本表不把这种情况升级为启动拒绝：
`deploy/compose.yaml` 以 `${VAR:-默认值}` 透传这些键，进程无法区分「操作者显式写了默认值」
与「Compose 替未设置变量转发默认值」，因此按值判定实现不了可靠的点名。操作者需要知道
的是：旋钮的作用域是所属运行体，装配该运行体的键为空时旋钮不会生效。

## 元守护

`tests/ci/test_configuration_prerequisites.py` 是本表的守护，做四件事：

1. **清单完整性**：扫描的四个 settings 模块里每个可选选择键（`SecretStr` / `Path` /
   `AnyHttpUrl` / `str` 且可为 `None`）必须在本表有一行，或在模块 `EXEMPT` 里有非空理由。
   新加一个 `*_DATABASE_URL` 或伴生键而不分类，守护直接红。
2. **证据存在**：每行的 `evidence` 指到具体测试函数或文档句子；改名或改文案都红，
   防止表里留着失效引用。
3. **静默行必须有处置**：`静默无效` 行的处置只能是 `已修 #<issue>` 或
   `已开 issue #<issue>`；`已修` 还要求证据是可执行测试。新增一个静默键而不处置即红。
4. **文档与模块同步**：本表与 `tests/ci/configuration_prerequisites.py` 的键、表现、处置
   逐行一致，避免出现「守护绿、文档旧」或反向的漂移。

它抓得住：未分类的新可选键、被删除或被改名后失效的证据、未处置的静默行、文档与模块漂移。
它抓不住：消息措辞是否真的点名（由被引用的单测断言）、从未在 settings 里声明过的隐式配对、
数值旋钮的作用域（见上节），以及 `deploy/compose.yaml` 之外平台注入的键。
