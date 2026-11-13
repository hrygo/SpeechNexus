---
title: 公开 TTS Job 正文上限实测（2026-11-14）
status: active
---

# 公开 TTS Job 正文上限实测

本记录是 issue #690 排查期间的过程证据，保留时间线、命令与原始输出；结论所引用的当前值以
同日主库 `origin/main`（`b6f3cf4d`）为准。测量只使用隔离数据库与合成数据，不改动生产代码。

## 结论

公开 TTS Job 的正文（`input.text`）在生产准入装配下的**有效上限是 4096 个码点**，而不是请求体
或持久化文档的字节上限：

- 4096 字符 → HTTP 202（生产准入装配经真实 HTTP 边界实测）；
- 4097 字符 → HTTP 400 `invalid_job_request`，服务端抛出
  `ValueError: Invalid speech text`。

同一装配下 200000 字符在更早的一层就被拒绝：请求体 600,166 字节超过默认
`job_receive_max_bytes = 262144`，返回 400 `invalid_job_request`，根本到不了准入。

根因是准入阶段用全文构造一次合成计划：`public_job_admission.py` 为 tts 构造
`SpeechPlan(text=全文)`，而 `speech/domain/speech_synthesis.py` 强制
`1 <= len(text) <= MAX_SPEECH_TEXT_CHARS`（4096）。执行侧还有第二道同名上限
（`execution-gateway` 的 `domain/tts.py`：`MAX_TTS_TEXT_CHARS = 4096`、
`MAX_TTS_TEXT_BYTES = 16384`）。

因此契约 `TtsJobInput.text.maxLength: 200000`（`contracts/speech-openapi.yaml`）在当前
「一个 Job = 一次合成」模型下不可交付。与此同时，四处 262144 字节上界今天彼此相等，且在
4096 字符上限下**永远不可能被任何契约合法 Job 触到**（最坏 27,591 字节，见「算术」），放宽它们
对生产没有收益，只会把单行文档上限抬到 832 KiB。本记录因此只交证据，不交放宽改动。

## 环境

- 基线：`origin/main` `b6f3cf4d8297b900d99a3c9a76ec16ef203628fa`，独立 worktree
  `fix/690-body-limit`，Python 3.14.9，uv 0.13.5。
- PostgreSQL 18.6：临时集群 `initdb --auth-local=trust --auth-host=reject --no-locale --encoding=UTF8`，
  Unix socket 端口 55439，独立目录，测试后销毁。
- MySQL 8（InnoDB 协调路径）：项目自带 `scripts/dev-env/mysql-service.sh` 启动的隔离容器，
  用于满足双数据库集成夹具的前提。
- 装配：
  - **旧式装配**（`tests/integration/test_public_jobs_http.py` 的 `jobs_db` 夹具，
    `PostgresPublicJobs` 不接准入池）——只覆盖请求体与文档字节链；
  - **生产准入装配**（`tests/integration/test_public_job_atomic_admission_pg.py` 的
    `admission_database` 夹具，`PostgresPublicJobs(admission_pool=…, require_admission=True)`）
    ——与生产组合根一致，覆盖配额、租约、准入事务。

## 时间线

| 阶段 | 动作 | 结果 |
| --- | --- | --- |
| 1 | 旧式装配按默认设置发 80000 / 100000 个中文字符 | 80000 → 202；100000 → 400 |
| 2 | 仅把 `job_receive_max_bytes` 在内存中调至 851968（不改源码）后重发 | 200000 → 400（文档上界）；200001 → 422（契约）；300000 → 400（请求体上界） |
| 3 | 直接 `submit` 200000 字符，取异常栈 | `ValueError: Invalid public Job request document`（`NewPublicJob.__post_init__`） |
| 4 | 起隔离 MySQL，跑通 `test_public_job_atomic_admission_pg.py`，确认生产准入装配可用 | 16 passed |
| 5 | 在生产准入装配下按 4096 / 4097 / 20000 / 200000 字符建 Job | 4096 → created；4097 与 20000 → `Invalid speech text`；200000 → 文档上界先触发 |
| 5b | 把同一装配接到 HTTP 边界，按 4096 / 4097 字符发 `POST /v1/nexus/jobs` | 4096 → 202；4097 → 400 `invalid_job_request` |
| 6 | 复核四处字节上界与契约上限 | 四处均为 262144 且彼此相等；同步路由契约 4096，异步 Job 契约 200000 |

## 原始输出

旧式装配，默认设置（请求体 = 紧凑 JSON 的 UTF-8 字节数）：

```text
PROBE default head chars=80000 wire=240142B -> 202 -
PROBE default head chars=100000 wire=300142B -> 400 invalid_job_request
```

同一装配，仅在内存中把 `job_receive_max_bytes` 调至 851968（反事实探针，不提交）：

```text
PROBE receive_limit=851968 chars=200000 wire=600142B -> 400 invalid_job_request
PROBE receive_limit=851968 chars=200001 wire=600145B -> 422 unsupported_parameter
PROBE receive_limit=851968 chars=300000 wire=900142B -> 400 invalid_job_request
```

200000 字符被 400 拒绝时，服务端在持久化前抛出：

```text
ValueError: Invalid public Job request document
  at speech/domain/public_jobs.py:250 (NewPublicJob.__post_init__)
```

生产准入装配（`PostgresPublicJobs(admission_pool=…, require_admission=True)`）：

```text
PROBE admission chars=4096 -> outcome=created error=None
PROBE admission chars=4097 raised ValueError: Invalid speech text
PROBE admission chars=20000 raised ValueError: Invalid speech text
PROBE admission chars=200000 raised ValueError: Invalid public Job request document
```

同一装配接到 HTTP 边界（`create_app` + `PublicJobs`，`POST /v1/nexus/jobs`）：

```text
PROBE http admission chars=4096 wire=12454B -> 202 -
PROBE http admission chars=4097 wire=12457B -> 400 invalid_job_request
```

4097 字符的异常来自 `speech/domain/speech_synthesis.py:197`：

```text
ValueError: Invalid speech text
```

说明：正文在 4097 到约 8.4 万–8.7 万（中文，视信封填充）或约 6.3 万–6.6 万（四字节码点）之间时，
规范文档仍在 262144 以内，400 由合成计划的 4096 上限触发；更长的正文在 `NewPublicJob` 处先被
262144 文档上界拦下。两道墙都使「>4096 字符」不可交付。

## 复现要点

两类探针都复用仓库现有的集成夹具，只新增一个临时测试文件；测量完成后删除，不进入分支：

```python
# 生产准入装配：复用 tests/integration/test_public_job_atomic_admission_pg.py 的夹具与 _seed/_store
from tests.integration.test_public_job_atomic_admission_pg import (
    admission_database, database, foundation, runtime_database, _seed, _store,
)

intent = {"workspace_id": WORKSPACE, "kind": "tts", "model": BINDING,
          "input": {"text": "中" * chars}, "output": {...}, "constraints": {...},
          "voice": {"id": VOICE, "revision": "2"}}
job = NewPublicJob(..., request_document=rfc8785.dumps(intent).decode("utf-8"), ...)
result = _store(admission_database).create(job)   # chars=4096 时 outcome=created
```

HTTP 边界的探针在 `admission_database` 夹具之上组装 `create_app(settings=…, jobs=PublicJobs(store, store))`，
请求体带 `workspace_id`（夹具只给 `workspace-test` 配额行），否则会因配额行缺失返回 503：

```python
service = PublicJobs(store, store)
app = create_app(settings=settings, jobs=service)
reply = await client.post("/v1/nexus/jobs", json={"workspace_id": WORKSPACE, "kind": "tts", ...})
```

旧式装配的探针复用 `tests/integration/test_public_jobs_http.py` 的 `jobs_db`、`_settings_for`、
`_app` 与 `_body`，经 ASGI 发请求；请求体字节数用
`len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())` 计量。

## 与代码的对应

| 层 | 位置 | 当前值 |
| --- | --- | --- |
| HTTP 请求体 | `settings.py` `job_receive_max_bytes` | 262144 |
| 领域文档（新建） | `speech/domain/public_jobs.py:248` | 262144 |
| 领域文档（重放读取） | `speech/domain/public_jobs.py:379` | 262144 |
| 持久化 CHECK | `speech/infrastructure/public_job_intent_schema.py:23` | 262144 |
| 准入合成计划 | `speech/domain/speech_synthesis.py:197`（`MAX_SPEECH_TEXT_CHARS`） | 4096 |
| 执行侧命令 | `execution-gateway` `domain/tts.py` | 4096 字符 / 16384 字节 |
| 同步路由契约 | `contracts/speech-openapi.yaml` `SpeechRequest.input.maxLength` | 4096 |
| 异步 Job 契约 | `contracts/speech-openapi.yaml` `TtsJobInput.text.maxLength` | 200000 |

生产创建路径最终调用 `build_public_job_admission`，它对 tts 用全文构造 `SpeechPlan`；计划校验失败
抛出的 `ValueError` 不在 `PostgresPublicJobs.create` 的捕获列表里，继续上抛到 HTTP 边界，
被映射成 400 `invalid_job_request`。

## 算术

正文按码点计数（契约 `maxLength`），服务端按 UTF-8 字节计量（请求体与文档）。在合成计划的
4096 码点上限下：

| 正文 | 正文字节（UTF-8） | 最大合法信封下的规范文档 |
| --- | --- | --- |
| 4096 个中文（3 字节/码点） | 12,288 | 21,076 |
| 4096 个四字节码点（最坏） | 16,384 | 27,591 |
| 200000 个中文（契约上限） | 600,000 | 608,788 |
| 200000 个四字节码点（契约上限） | 800,000 | 811,207 |

（信封取契约各字段最大长度：`model` 128、`client_reference` 256、`language` 35、
`instructions` 4096、三个 128 位标识与 19 位修订号、planner 与 constraints 全字段。）

- 4096 码点的最坏文档 27,591 字节，距 262144 还有 9.5 倍余量；
- 契约上限 200000 码点的文档 811,207 字节，超过 262144 已达 3.1 倍；
- 执行侧 16384 字节上限恰好等于 4096 × 4，与 4096 码点上限同源。

所以：任何**能过合成计划校验**的 Job，其文档最多 27,591 字节，四处 262144 上界没有一次机会触发。
把上界放宽到 851968 只在「计划上限被提高」之后才有意义，而那属于另一片设计变更。

## 未实施项

以下放宽改动在本地验证后已全部撤回，分支上不留生产代码改动：

| 项 | 位置 | 原拟值 | 现状 |
| --- | --- | --- | --- |
| 设置默认值 | `backend/src/speechnexus_backend/settings.py:174` | 851968 | 保持 262144 |
| 领域文档常量（新建） | `speech/domain/public_jobs.py:248` | 851968 | 保持 262144 |
| 领域文档常量（重放） | `speech/domain/public_jobs.py:379` | 851968 | 保持 262144 |
| 持久化 CHECK | `public_job_intent_schema.py:23` | 851968 | 保持 262144 |
| 新迁移 | `0054_public_job_intent_bytes` | 重建 CHECK | 未提交（文件已删除） |

撤回理由：在 4096 码点上限下，四处上界不可达；放宽不改变任何可观察行为，却把单行文档上限
抬到 832 KiB、增加 `speech_job_intents` 的膨胀上限，并引入一次需要 ACCESS EXCLUSIVE 锁和全表
校验的迁移。按「宁可宣称得少」，这类零收益上界放宽不应合入。

## 未验证项与推断

- 未接真实引擎：执行侧 4096/16384 的拒绝行为来自网关领域代码与文档，没有实跑引擎；本记录
  只证明「后端准入在 4097 处已经拒绝」，不证明更长的正文在引擎侧能成功。
- 准入路径的 HTTP 结果只在 `workspace-test` 的合成配额下测过；真实配额与并发下的 429/503
  未实测，本记录不据此推断容量结论。
- 未做负载/容量压测：超长正文的存储与内存开销没有实测数据。
- 200000 字符在旧式装配下的 400 来自文档上界，这一点由异常栈确认；「若上界放宽会走到计划
  上限」的推断由 4097/20000 的实测支持，但没有在放宽后实跑 200000 的准入路径（本记录不做
  该放宽）。
- 契约 `maxLength` 是码点还是字符，行文按 OpenAPI 语义视为码点；正文按 UTF-8 字节计量，
  两者只在非 ASCII 时不同，本记录已分别标注。

## 后续能力

把长正文按 planner 切分为多个合成单元（或改为文本资产 + 切分）后，Job 的 200000 字符承诺才可能
成立；届时再按实际切分上限重新推导请求体与文档的字节预算，而不是提前放宽。该能力不在本片范围。
