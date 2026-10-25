---
title: "团队 C 交接：内网 LLM 接线与纪要派发 worker"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 团队 C 交接：内网 LLM 接线与纪要派发 worker

本轮把「默认关闭的内网 LLM、纪要任务」从"代码存在但部署无从启用"补成可配置、可运行、有真实模型证据的闭环。C06 是本轮主责单元，同时为 C05 会议纪要、C07 依赖 LLM 的切片提供同一绑定。

## 基线

- `base_sha` = `head_sha` = `4b08c396c0fe8fc43c7e85683e534befab6bb22b`（未提交、未推送，与上一轮冻结单元相同）。
- 上一冻结单元：`C08-independent-save-ui`，累计补丁 `076481830e175e0fd1919a4ff6a0f93696332f7b155a8dd95c9cf0d79bb7c954`，438 个路径。
- 本单元：`C06-minutes-worker-and-intranet-llm`。新增 3 个路径（本文件、`test_workbench_minutes_dispatch.py`、`test_workbench_minutes_live.py`），修改 5 个路径（`runtime.py`、`runtime_settings.py`、`test_workbench_runtime_settings.py`、`test_workbench_minutes_tasks_postgres.py`、本计划文档）。最终累计补丁哈希与逐文件哈希见 `.runtime/team-c/team-c-current.manifest.json`。

## 本轮解决的问题

接线前的事实：`OpenAiTextLlm` 适配器与 `MinutesTasks` 服务都已存在且有单元测试，但**生产代码里没有任何一处构造它们**；`WorkbenchSettings` 没有 LLM 字段，`runtime.py` 明确写着"不注入 LLM"，因此：

- 部署即使有合规内网模型，也无法配置它（没有配置面）；
- 即使配置了也没有调用点（`TextLlm.generate` 在生产代码中零调用）；
- 纪要任务只能停在 `queued`（`WorkbenchMinutes.generate` 与 `MinutesTasks.execute` 都不在运行路径上）。

本轮补齐三件事：部署配置面、组合根注入、派发 worker。

## 改动

- `runtime_settings.py`：新增默认关闭的 LLM 配置面——`llm_endpoint`（必须是**完整的** `/v1/chat/completions` URL）、`llm_model`、`llm_template_revision`、`llm_system_prompt`（C 自有默认模板，可被部署覆盖）、`llm_api_key`（`SecretStr`、`repr=False`）、`llm_qualification_reference`、`llm_deadline_seconds`（默认 30，上限 120），以及 `minutes_interval_seconds`（派发间隔，默认 5）。
  - 三者（endpoint/model/qualification）必须**同时**给出或全缺；`llm_configuration()` 负责装配，并由 `model_validator` 在**启动前**调用一次，端点与 profile 的合法性由适配器自身裁决（单一事实来源，不复制规则）。
  - 因此公网主机、错误路径、非本机 http、带查询串的端点都在启动时被拒（`ValidationError`），并且错误文本不包含被拒的输入串（`hide_input_in_errors` 保持生效）。
- `runtime.py`：`llm = OpenAiTextLlm(settings.llm_configuration())`；同一绑定注入 `WorkbenchMeetings(...)`（纪要意图记录 profile/binding，并让 status 上报）与 `WorkbenchAssistant(...)`（默认关闭时给稳定 `llm_disabled`，绝不接受客户端指定模型）。
- 纪要 worker：新增 `_dispatch_minutes_once`（读一页队列、逐个执行）与 `_dispatch_minutes`（按 `minutes_interval_seconds` 循环）。仅在 `llm.profile is not None` 时随 lifespan 启动，随 lifespan 取消。批量上限 `_MINUTES_BATCH = 8`，逐任务捕获异常；`MinutesTaskConflictError` 表示别的实例已持有 claim，按 info 记录而不是异常噪音。每轮重新读持久队列，因此重启、单轮失败或人工取消都不会留下"已 claim 但没有结果"的工作。

## 真实证据（本轮新增，均为真实运行）

环境：本机 oMLX `http://127.0.0.1:8000/v1/chat/completions`，模型 `KAT-Coder-V2.5-Dev-OptiQ-4bit`；隔离 Unix-socket-only PostgreSQL 探针（`speechnexus_workbench_test` / `workbench_test_owner`）。

| 证据 | 命令/环境 | 结果 |
| --- | --- | --- |
| 单元 + 契约 | `uv run pytest tests/unit tests/contract -q` | **3997 通过**（90.96s；较上轮 +7：派发 4 项、设置 3 项） |
| 隔离 PG 全量探针 | `workbench-pg-probe.py --pg-bin … --workbench-runtime`（含 MinIO 三个变量） | **379 通过**（81.14s；较上轮 +1＝新增派发集成用例） |
| 纪要集成（合成 LLM，真实 PG） | `uv run pytest tests/integration/test_workbench_minutes_tasks_postgres.py -q` | **32 通过** |
| 真实模型（产品路径） | `uv run pytest tests/integration/test_workbench_minutes_live.py -q` | **2 通过**（19.27s） |
| 静态/契约 | `ruff check .`、`mypy`（517 文件）、`lint-imports`（7 kept/0 broken）、`make contracts-check` | 全绿 |

两条 live 用例的覆盖边界不同，缺一不可：

1. `test_a_real_local_model_completes_a_queued_minutes_task`：产品服务路径入队 → 单次派发 → 真实模型 → 真实 PG 落库，断言 `succeeded`、正文非空、profile 与配置一致、attempt 的 `usage.input_tokens/output_tokens > 0` 且 `completion_confirmed`。
2. `test_the_serving_lifespan_dispatch_loop_completes_a_queued_task`：用**真实 `create_runtime_app` lifespan**（真实启动检查、真实组合根）拥有派发循环，测试只负责入队；随后断言 `GET /v1/workbench/status` 返回 `llm_enabled: true` 与 `minutes_profile.llm_model/template_revision`，再断言任务 `succeeded`。这才证明"部署消费者"而不只是"函数可调用"。

## 环境发现（重要，供 A/B 与后续单元复用）

- **CLIProxyAPI 别名不可直接作为 C 的 pinned 模型**：经 `127.0.0.1:8317/v1` 请求别名 `omlx-kat-coder-2.5` 能成功，但响应 `model` 是上游原生名 `KAT-Coder-V2.5-Dev-OptiQ-4bit`；而 C 的适配器要求 `response.model == profile.model`（对应答必须来自被 pin 的模型）。改用原生名请求代理则 400 `model_not_found`（代理只暴露别名）。因此部署配置选择**直连本机 oMLX**：响应 `model` 逐字匹配，且仍在同一台机器内，不触碰 ADR 0003 的内容网络边界。
- 代理侧 `omlx-*` 上游指向 `http://127.0.0.1:8000/v1`，即别名路径本身也是本机推理；只是"响应模型名"这一条契约不满足。若将来要在代理上启用，需要代理侧保留被请求的模型名（owner：部署/代理维护者），**不应**放宽 C 的 pinned 校验。
- 该模型默认开启 thinking：16 token 预算会被 reasoning 全部吃光（`finish_reason: length`、`content: None`）。profile 默认 2048 输出 token 足够；暖机后单次耗时约 0.85s，冷加载约 7s（`model_load_duration: 5.48`）。
- 本轮的少量真实调用只用于连通、预算与落库验证；**不声明**纪要质量，也未做批量或负载评价。

## 未验证与遗留

- 仍未接入真实 A/B：真实语音服务、真实 ASR/TTS 模型、真实设备、Linux 后端、GPU 与生产迁移/部署均未取得证据，`generateRenderUnit` 与三条下载/事件流操作仍未绑定（A 侧）。
- 助手切片：冻结契约里没有 C 侧文本生成操作，`WorkbenchAssistant` 对 LLM 的作用是"启用/禁用 + binding 记录"，其回答文本不在 C 的契约面内；本轮不擅自扩张协议。
- 派发循环按批串行（上限 8），单实例内不会并发堆积；推理期间不持有数据库事务（store 仍是短事务）。多实例情形由 `claim` 的 attempt token 保证只落一个结果，竞争以 info 记录。
- 未做：真实浏览器下的 LLM 启用态界面证据、真实 A 制品保存、导出下载与事件流。

## 下一接入动作

1. 等 A 绑定三条下载/事件流操作与非 JSON 响应体 → 接导出下载与已保存副本下载。
2. 等 A Job API 的 `generateRenderUnit` → 接生成派发与代次写入。
3. 等 A06/B05 → 音色候选/试听/校验/发布。
4. 真实浏览器/设备与 Linux/GPU 资格（C08）、生产迁移与清理 DSN 注入（A07）继续。
