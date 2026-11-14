---
title: 无配额工作区不再伪装成 503 的实测（2026-11-14）
status: active
---

# 无配额工作区不再伪装成 503 的实测

本记录是 issue #702 的实施与反例证据，保留基线、命令与原始输出。测量只使用隔离数据库与合成
数据；默认工作区 id（`workspace-default`）与配额行的产生方式均未改动。记录同时包含非作者审查
（[PR #732 审查](../reviews/2026-11-14-workspace-quota-refusal.md)）后 M1/M2/M4 的修正：
工作台不再把该拒绝登记为 `terminal`，契约把两种存储故障各自的 `retryable` 说准，多行分支标明
在现有主键下不可达。

## 结论

默认部署里 `speech_workspaces` 由 asset 迁移播种 `workspace-default`，而
`workspace_quotas` 只由引擎注册写入（`engine_registration.py` 的 `_ensure_quota`）。因此
「工作区行存在、但没有配额行」时，准入读不到配额。此前该情形返回 503
`job_unavailable`（"Job storage is unavailable."），把确定性配置缺失表述成可重试的存储
outage。本 issue 之后：

- 契约的 `createJob` 声明省略 `workspace_id` 时的默认工作区
  （`default: workspace-default`），并在 `JobCreateBase.workspace_id` 字段说明与操作描述里
  写明该行为及其三个确定性 422 回答；
- 「工作区存在但无配额行」返回 **422 `workspace_not_configured`**（`invalid_request_error`，
  `retryable: false`），零行写入；该拒绝是「补上配额前重试同一请求不会成功」，
  **补齐后同一幂等键下的同一请求即可准入**，所以它不是终态拒绝；
- 存储侧的两种失败各归其码：查询被拒或数据无法归并 → 503 `job_unavailable`
  （`retryable: false`）；连接级失败/事务内超时 → 503 `job_outcome_unknown`
  （`retryable: true`）。两个答案的码与重试承诺都不同，双向都有用例守住；
- 工作台把该 422 登记为 `intent-kept`（保留可重放意图，并显示具体状态码与错误码），既不消耗
  持久意图，也不与 503 存储 outage 混为一谈；
- 不自动创建配额行：准入语义不变，只让原因可区分。

## 环境

- 基线：`origin/main` `87449a69847748738a719a1f2f47edea4bc7f5a9`，独立 worktree
  `/private/tmp/sn731`（分支 `fix/702-quota-row`），CPython 3.14.9、uv 0.13.5、
  系统 node v26.12.1、pnpm 12.14.0、httpx 0.28.1。
- PostgreSQL 18.6：临时集群
  `initdb --auth-local=trust --auth-host=trust --no-locale --encoding=UTF8`，
  仅监听 127.0.0.1:55439，独立目录，测量后销毁。
- MySQL 8.4.8：项目既有隔离容器 `speechnexus-coordination-mysql`（127.0.0.1:55434），
  仅用于满足双数据库集成夹具的前提。
- 装配：`tests/integration/test_public_job_admission_http_pg.py` 的 `admission_database`
  夹具，**生产准入装配**（`PostgresPublicJobs(admission_pool=…, require_admission=True)`），
  经 **真 TCP HTTP**（uvicorn 127.0.0.1:18081 + httpx）发请求，`create_app` 与组合根一致。

## 真实状态码/错误码

| 状态 | 触发 | status | code | retryable | 行写入 |
| --- | --- | --- | --- | --- | --- |
| 工作区无配额行（省略 workspace → `workspace-default`） | 默认部署，未注册引擎 | 422 | `workspace_not_configured` | false | 0 |
| 工作区无配额行（显式命名该工作区） | 同上 | 422 | `workspace_not_configured` | false | 0 |
| 无 `speech_workspaces` 行 | 命名不存在的工作区 | 422 | `workspace_not_found` | false | 0 |
| 存储查询被拒 | 撤销准入角色对 `workspace_quotas` 的 SELECT | 503 | `job_unavailable` | false | 0 |
| 连接级失败 | 终止准入角色已池化的会话，下一次 create 取到死连接 | 503 | `job_outcome_unknown` | true | 0 |
| 工作区存在且有配额行 | 同一无 workspace 请求体 | 202 | — | — | +10 行、配额 `held_attempts` +1 |

「无 `speech_workspaces` 行」在进入准入事务前就由 `PublicJobs._prepare_job` 的 workspace
读取判为 `workspace_not_found`，因此不会到达配额分支；配额 JOIN 的零行在应用层预检之后只
可能表示「工作区行存在、无配额行」。探针覆盖前四行；「连接级失败」一行由
`tests/integration/test_public_job_admission_http_pg.py` 的
`test_a_killed_admission_connection_is_a_retryable_unknown_outcome` 实测（终止服务端会话后
503 `job_outcome_unknown`、`retryable: true`、零写入；连接池换新连接后同一键 202，同键重放
仍是同一 Job），因为探针脚本已删除、不再随提交保存。

## 原始输出

同一声明/装配，真 TCP HTTP：

```text
PROBE transport=real-tcp httpx=0.28.1
PROBE unnamed/default-no-quota   status=422 code=workspace_not_configured retryable=False job=- message='The workspace has no provisioned Job quota.'
PROBE named/exists-no-quota      status=422 code=workspace_not_configured retryable=False job=- message='The workspace has no provisioned Job quota.'
PROBE named/no-workspace-row     status=422 code=workspace_not_found retryable=False job=- message='The workspace was not found.'
PROBE storage-read-failed        status=503 code=job_unavailable retryable=False job=- message='Job storage is unavailable.'
PROBE quota-present              status=202 code=- retryable=- job=job-0f21aff323684de387c7a888101ecc63 message='-'
PROBE quota-present-same-key     status=202 code=- retryable=- job=job-0f21aff323684de387c7a888101ecc63 message='-'
PROBE default_quota [{"held_attempts": 1}]
```

- 前两行是同一缺失配额条件的两条入口（省略/显式 `workspace_id`），答案一致且都不写行。
- `storage-read-failed` 让同一请求走真正的 `psycopg` 失败分支，仍是 503
  `job_unavailable`，不是 422。另一种存储失败（连接级）不是这个码：终止池化会话后
  create 取到死连接，`psycopg.OperationalError` 映射为 `outcome_unknown` → 503
  `job_outcome_unknown`（`retryable: true`），由集成用例实测，见下节。
- `quota-present` 与 `quota-present-same-key` 返回同一个 `job-…`，且只保留一次 attempt
  （`held_attempts=1`），证明正常路径没有被误伤，重放也不会二次准入。

## 反例（变异实验，均已还原，未进入提交）

| 变异 | 改动 | 期望 | 实际 |
| --- | --- | --- | --- |
| A | `public_job_admission.py` 的 `_quota` 零行增加改回 `job_unavailable` | 单元与跨层用例红 | 红：单元 `assert 'job_unavailable' == 'workspace_not_configured'`；跨层 `assert 503 == 422`（`test_create_without_a_provisioned_workspace_quota_is_refused_without_rows[postgresql]`） |
| B | `public_jobs_postgres.py` 的 `except psycopg.Error` 分支改报 `workspace_not_configured` | 存储真故障用例红 | 红：`assert 422 == 503`，body 为 422 `workspace_not_configured`（同一跨层用例的 storage-fault 段） |
| C | 不改代码：工作区有配额行 | 仍 202 | 绿：202 + 同键重放同 `job`，`held_attempts=1` |
| D | 工作台把该 422 记为 `unknown-outcome` | 工作台对应用例红 | 红 2 例（`2 failed / 871 passed`）：`422 workspace_not_configured is intent-kept` 与专用「keeps a missing workspace quota replayable」 |
| E | 工作台把该 422 改回 `terminal` | 工作台对应用例红 | 红 2 例（`2 failed / 871 passed`）：同两条用例——`unknown` 与「补齐配额后同一键准入」断言失败，即会把可重放意图结清 |

变异 A/B 覆盖「少判」与「多判」两个方向：A 让确定性缺失退回 outage，B 让真 outage 冒充
配置缺失。D/E 覆盖工作台的两个错误方向：归为「结果未知」或归为「终态」都会被拒；正确处置是
保留意图可重放，同时显示具体状态码/错误码。

审查后追加的两条映射反例（M2）：

| 变异 | 改动 | 实际 |
| --- | --- | --- |
| F | 把该 422 从 `_ADMISSION_REJECTION_CODES` 移除 | 红 2 例：单元 `test_the_new_code_reaches_http_as_a_refusal_not_the_unavailable_fallback`（落到 `JobRejectedError`，见 traceback `public_jobs.py:1527`）与跨层 `test_create_without_a_provisioned_workspace_quota_is_refused_without_rows[postgresql]` |
| G | 连接级失败不再走 `outcome_unknown`（从 `OperationalError` 的捕获里移除，落回 `storage_rejected`） | 红 1 例：`test_a_killed_admission_connection_is_a_retryable_unknown_outcome[postgresql]`；池日志同时给出机制证据 `discarding closed connection: <psycopg.Connection [BAD]>` |

## 测试与门禁

| 命令 | 结果 |
| --- | --- |
| `uv run pytest tests/contract -q` | 3740 passed |
| `uv run pytest tests/integration/test_public_job_admission_http_pg.py -q` | 6 passed（含连接级失败用例） |
| 相邻公开 Job 套件（atomic admission / binding pin / cancellation / list / pin http / retry sse / public_jobs_http / progress，PG+InnoDB 夹具） | 96 passed（含准入套件共 102） |
| `uv run pytest tests/unit/backend -q` | 3050 passed |
| `cd web/workbench && pnpm test` | 62 files / 861 tests passed（合并树；= main 的 858 + 本片 3） |
| `make contracts-drift` | up to date |
| `check-route-conformance.py` | OK：每条注册路由与契约匹配，无新偏差 |
| `CONTRACT_BASE_REF=origin/main make contracts-speech-changes` | passed（`0.1.0.dev18 -> 0.1.0.dev18`，无结构性 finding） |
| `uv run ruff check .` / `ruff format --check .` | passed / 1840 files formatted |
| `uv run mypy --platform linux` | Success: 1449 source files |
| `uv run lint-imports` | 9 contracts kept, 0 broken |

## 契约声明的选择：为什么默认值写在描述里

契约在 `JobCreateBase.workspace_id` 的字段说明和 `createJob` 描述里声明
「`default: workspace-default`：省略字段选择该默认工作区，响应回显解析后的
`workspace_id`」。没有给该字段加 JSON Schema `default` 关键字：那会改变每个解析后的
`JobCreateRequest` oneOf 变体，`check-http-contract-changes.py` 对此类 exclusive-union
变化要求显式兼容证明，而放行此类变化需要修改 `scripts/dev-env/check-*.py` 的审批表，属于本
任务边界之外。描述文本与 schema 默认值表达同一语义，且不改变请求结构的兼容性判定。

## 边界与未验证项

- 实测使用真实 PG 与真 TCP HTTP，但没有覆盖网络分区、连接丢失或 failover；
  `storage-read-failed` 用撤销 SELECT 制造真实读取失败，连接级用例用终止服务端会话制造死连接，
  都不是网络故障注入。
- 没有验证 Workbench 之外的消费者（例如其他客户端）如何处理新错误码；工作台已按「保留可重放
  意图」处置并有 D/E 两条反例。
- `voice_design`/`voice_clone` 的 draft 生成走同一准入，当前把该拒绝折叠为既有的
  `422 voice_draft_input_unavailable`（非重试）；本片未新增该表面的错误码，文案仍沿用该
  表面既有措辞。
- `503 job_unavailable` 的 `retryable` 维持既有评审决定的 `false`（该码同时服务读路径，
  存在永久不可恢复的读；连接级失败由 `job_outcome_unknown` 承担重试语义）。审查后契约措辞已按
  此收窄，不再声称所有存储故障都不可重试。
- 工作台在「拒绝后、重放前」重载时仍显示未知态（保守方向）：本片未引入「已结算且可重放」的
  第四态，代价与理由见审查报告 M1 的选项 (a)。
