---
title: "PR #857 非作者审查：Job 准入可观察面（#845）"
status: active
version: "0.1.0"
date: 2026-11-16
---

# PR #857 非作者审查：Job 准入可观察面（#845）

审查对象：PR [#857](https://github.com/hrygo/SpeechNexus/pull/857)
`fix/845-admission-observability`，关联 issue [#845](https://github.com/hrygo/SpeechNexus/issues/845)。

## 0. 基线漂移：被指定的 head 已被 force-push

派单指定 head `bc7c4dc3`、基线 `origin/main` = `c7e43f9a`。审查过程中实测该 head
**已被 force-push 掉**：

```
$ git merge-base --is-ancestor bc7c4dc3 555f6290   → 失败（orphaned）
$ git ls-remote origin refs/pull/857/head          → 555f629079077c4ee3fc7fc7871e13ea43004665
$ gh pr view 857 --json headRefOid,baseRefOid       → head 555f6290, base 45db3910
```

远端现在的三个 commit：`ad4959ab`（主体）→ `0a60c9b5`（ruff 格式）→ `555f6290`
（**把轮询者移进 `admission-watch` profile**）。旧的两个 commit（`01260bec`、`bc7c4dc3`）
已不可达。`origin/main` 也从 `c7e43f9a` 前进到 `45db3910`（含 #855、#856）。

实测两版之间的差异：

```
$ git diff bc7c4dc3 555f6290 --stat
（产品代码、契约、单元/契约/集成测试 —— 0 个文件）
deploy/compose.yaml                                | 42 +++---
deploy/compose.env.example                         | 13 +--
docs/operations/engine-readiness-observability.md  | 69 +++---
tests/ci/test_reference_deploy_job_admission_watch.py | 67 +++---
(+ 4 个文件来自主干 #855/#856，非本 PR)
```

结论：**产品代码、`contracts/speech-openapi.yaml`、四类 cause 的枚举与映射、
`public_jobs_postgres` 的读取查询、以及全部单元/契约/集成测试在两版之间逐字节相同**；
返修只动了「轮询者进不进 base」这层装配与文档。因此下文所有对 product 语义的结论对
两个版本同时成立；凡是只对某一版成立的，逐条注明。

**本报告只审 PR #857 自身**（不含主干 #855/#856）。审查分支 `review/859-845` 基于
`origin/main` = `45db3910`，本报告是它与 `origin/main` 的唯一差异。

## 1. 结论

**不能合。** 三条阻断项都在「报告说的类」与「创建实际返回的码」之间对不上——正是
#845 要消灭的那类缺陷，只是从 `/readyz` 换到了新路由自己身上：

| 阻断 | 事实 |
| --- | --- |
| **B1** | 有 ready 实例、有有效回执、但**池不是 `active`** 时：报告 `admitting`，创建 `429 job_capacity_exhausted`。真实 PostgreSQL 上实测（PROBE-C）。 |
| **B2** | **没有 ready 实例**时：报告 `engine_receipt_expired` 并在 `detail` 里断言「Admission answers engine_not_ready」，创建实际 `429 job_capacity_exhausted`。契约同处也写错（`contracts/speech-openapi.yaml:1182`）。实测（PROBE-A/PROBE-B）。 |
| **B3** | 契约的 cause→码表没覆盖 `engine_state_mismatch`、`engine_resource_disabled` 两个 503 码的持续态；反向比对无任何测试或守卫（第 4 节）。 |

其中 B2 是**纯文档/契约级**错误（产品行为按 #828 的既有决定是对的：无实例=容量拒绝），
改一句话即可；B1、B3 是语义缺陷，需要产品决定。

## 2. `job-admission-watch` 该不该进 base 拓扑

### 2.1 镜像与挂载：不是 `python:3.14-alpine` + bind-mount

派单担心的形态不成立。两版都用仓库自己的构建：

```
deploy/compose.yaml:729   build: *python-build        # context: .., deploy/images/python.Dockerfile
deploy/compose.yaml:730   command: ["speechnexus-admission-watch"]
backend/pyproject.toml    speechnexus-admission-watch = "speechnexus_backend.admission_watch:main"
```

入口是**装在镜像里的 console script**，没有 `volumes:`，所以「运维是否从仓库根目录执行
compose」不构成假设（`context: ..` 相对 compose 文件解析）。`read_only: true` +
`/run/admission-watch` tmpfs 让只读根成立。**这一点作者做对了，且返修后文档
`docs/operations/engine-readiness-observability.md:71-74` 已把「同一个仓库镜像 / 无 bind-mount /
不依赖 cwd」写成事实。**

### 2.2 base vs opt-in：返修版的选择是对的，但默认部署仍无信号

- **旧 head（`bc7c4dc3`，在 base）**：不该进 base。理由与作者返修后自己给出的一致——
  一个「缺席也不会让任何东西失败」的观测者写进 base，等于把一个**沉默的缺席**伪装成
  「装好了」。`restart: "no"` 让它在配置非法（`EXIT_INVALID = 4`）或任何未捕获异常时直接
  `Exited`，此时 `docker ps` 里它**根本不出现**（不是 `unhealthy`），栈看上去只是少了
  一行，主链路毫无影响——这就是 #845 的形态换了方向。
- **现 head（`555f6290`，`profiles: ["admission-watch"]`）**：判断正确，我建议**维持**。
  实证：`docker compose ... config --services` 在 base 下是
  `api gateway minio minio-init postgres`，加 `--profile admission-watch` 才是六件套；
  base 拓扑与 `origin/main` 完全一致。
- 残余（不阻断，但必须在 PR 里说清）：参考部署**默认** `docker compose up` 仍然没有任何
  准入信号。作者用「验收清单」把「必须 `--profile admission-watch` 且 `running/healthy`」
  变成文档级要求（`docs/operations/engine-readiness-observability.md:137-141`），并由
  `tests/ci/test_reference_deploy_job_admission_watch.py` 钉住这段文字存在。这是**文档约束，
  不是部署约束**：漏掉 profile 不会让任何测试或容器变红。

**必须修（文档归属缺口）**：`deploy/README.md` 零改动，而它第 19 行正是
「base `config --services` set is intentionally limited to:」那一份清单，第 25 行起是
profile 清单——新增 profile **没有出现在运维主入口**。同时 `grep -rn
"engine-readiness-observability" --include=*.md .` = **0 命中**：新运维文档是**孤儿文档**，
`docs/README.md`、`docs/operations/README.md`、`deploy/README.md` 都不指向它，只有
`tests/ci` 按路径读它。`scripts/dev-env/check-doc-refs.py` 只查出链、不查入链，所以没有任何
门禁会报。PR 正文把 `deploy/README.md` 让给 #823，理由成立（措辞被另一张卡的测试钉住），
但**孤儿文档**是自己的问题：至少把新文档挂到 `docs/operations/README.md` 与
`docs/README.md` 索引里一行。

### 2.3 healthcheck 语义：确实又把两种语义压进同一个 bit

`admission_watch.py:225-252` 的 `--healthcheck` 只有在「最近一次观测是 `admitting`
且不早于 `max(2×interval, 5)` 秒」时返回 0，其余全部返回 1：

| 实际状态 | `docker ps` | 可分辨来源 |
| --- | --- | --- |
| 创建会被拒（`degraded`） | `Up (unhealthy)` | 日志 `state=degraded` + `causes` |
| API 读不到（`unreachable`） | `Up (unhealthy)` | 日志 `state=unreachable` |
| Job 面没装配（`unavailable`） | `Up (unhealthy)` | 日志 `state=unavailable` |
| 轮询循环卡死（状态文件过期） | `Up (unhealthy)` | healthcheck 输出 `reason` |
| 轮询进程已退出 | **不出现在 `docker ps`** | 只能 `docker compose ps -a` |
| 部署还在冷启动（API 未起） | `Up (unhealthy)` | 无，纯噪声 |

所以：**只用 `docker ps` 分不清**，这正是 #840/#845 的缺陷族；但**可查**——作者返修后已在
`docs/operations/engine-readiness-observability.md:115-127` 明确写出「`unhealthy` 只说明
不可准入（含读不到），不说明容器坏了；`docker ps` 不区分这两件事」并给出 `docker inspect`
取 health log 的具体命令。这比把语义含糊留在注释里强得多，我认为**可接受**；如果要把两件事
真正拆开，唯一干净的做法是 healthcheck 只回答「观测者活着且在读」（`degraded` 也算 healthy），
把「现在建不了 Job」交给日志/指标消费——代价是 `docker ps` 不再兼任 #845 的告警位，需要另行决定。

### 2.4 「日志里有 `job_admission_transition`」只对一半情况成立

代码是对的：`admission_watch.py:281` 只在 `previous is not None and state != previous` 时
打 transition 事件。因此**部署启动时就已经降级，永远不会产生 transition 行**——只有每轮
`job_admission_observation`（含 `state` 与 `causes`）。所以「有 transition 事件」不能作为
「降级会被看到」的论据；能作为论据的是「每轮观测行都带 `state`+`causes`」。

### 2.5 文档事实错误（必须修）

`docs/operations/engine-readiness-observability.md:113` 写：

> 观测进程死亡时容器停在 `Exited(4)` 之类的终态而不是重启循环，healthcheck 也会因为状态文件过期而变红。

**这一句是错的**，而且与自己第 140 行矛盾（140 行写「它是 `Exited (N)` 或者根本不出现」是对的）。
Docker 只在容器主进程存活期间执行 healthcheck；主进程退出后容器进入 exited 状态，
healthcheck 不再被评估，`docker ps` 也不再列出它——不会「变红」，是**消失**。
这正是 2.2 里「base 里放一个可以沉默缺席的观测者」的同一条论据，被作者用在 compose 注释里
（`deploy/compose.yaml:8-15`），却没在运维文档里对齐。

另有一处计数错误：`docs/operations/engine-readiness-observability.md:36` 标题写
「三类机器可读原因」，表里是**四类**；第 169 行同样写「三类原因」。`causes` 枚举是四值。

### 2.6 冷启动噪声（不阻断，建议修）

缺少 `depends_on` 让轮询者在 `api` 还没起来时先报 `unreachable` → `unhealthy`。
`start_period: 5s` + `interval: 30s` + `retries: 3` 意味着冷启动约 95 秒后即被判 `unhealthy`，
而 `deploy/compose.yaml:264-276` 里 `api` 自己的 healthcheck 是 `retries: 20 × 5s`
（约 100 秒）+ `start_period: 10s`——两个预算几乎相等，正常冷启动很可能先看到一轮
误报。`tests/ci/test_reference_deploy_job_admission_watch.py` 明确**禁止** `depends_on`
（`test_the_poller_cannot_restart_anything`），但 `depends_on: api: {condition: service_started}`
只是启动顺序，不是「等它报告的那个条件」；把这条禁令写成无例外的硬规则，挡住了唯一一个
零成本的减噪手段。建议：放宽为「不得依赖轮询者自身、不得 `condition: service_healthy` 观测面」，
或把 `start_period` 提到 ≥ 120s。

## 3. 新路由是否真的消除缺陷

### 3.1 三个断言的层次是对的

派单问「三个断言断言的是 HTTP 响应还是内部对象」。实测：**真实 HTTP 响应**。
`tests/integration/test_job_admission_readiness_pg.py` 用
`AsyncClient(transport=ASGITransport(app=create_app(...)))` 装配生产组合根，跑真库：

```
$ SPEECHNEXUS_TEST_DATABASE_URL=... SPEECHNEXUS_TEST_MYSQL_URL=... \
  pytest tests/integration/test_job_admission_readiness_pg.py -q
.....                                                                    [100%]
5 passed in 3.13s
```

其中 `test_an_elapsed_receipt_degrades_the_report_while_readyz_stays_200`
（`tests/integration/test_job_admission_readiness_pg.py:218-275`）在同一次用例里断言：
`/readyz` = 200、报告 = 200 + `degraded` + `causes == ["engine_receipt_expired"]`、
`POST /v1/nexus/jobs` = 503 + `("engine_not_ready", retryable=False)`、`speech_jobs` 行数为 0，
再把回执写回后同一 Idempotency-Key 得 202。这是**同一部署上的三层对照**，不是内部对象断言。
契约测试里 `test_every_cause_names_a_create_code_the_create_operation_declares` 只用
`job_error(code)`（错误渲染器）而非创建路径，但那是契约层守卫，层次本身是合理的。

### 3.2 「读不到」与「读到了降级」确实可区分

- 报告路径：`public_jobs.py:1115-1136` 把 `PublicJobUnavailableError` 转成
  `storage_unavailable_readiness`，即 **200 + `degraded` + cause=`storage_unavailable`**，
  不是 `admitting`；Job 面未装配时整条路由 503 `job_unavailable`（与其他 Job 路由一致）。
  因此「报告坏了」最坏是 503，「读到了降级」是 200+body——**两个 HTTP 状态就把两件事分开**。
- 轮询者路径：`admission_watch.py:51-55` 用 `0/1/2/3/4` 五个退出码区分
  `admitting` / `degraded` / `unreachable` / `unavailable` / 配置非法，与文档
  `docs/operations/engine-readiness-observability.md:94-104` 一致。**这一条作者做对了，
  派单里担心的「也 200 + degraded 造成混淆」不成立。**

### 3.3 但缺陷只是**部分**消除

报告的 `degraded` 判据只有三条（`job_admission_readiness.py:335-347`）：
quota 行缺失、没有任何可用回执、quota 或 slot 满载。除此之外任何让创建持续失败的部署态，
报告都是 `admitting`。真实 PostgreSQL 实测三例（探针文件用后即删，`git status --porcelain` 为空）：

```
PROBE-A  无 ready 实例
  报告: status=degraded causes=['engine_receipt_expired']
        detail="... Admission answers engine_not_ready while no usable receipt exists."
  创建: 429 {"code":"job_capacity_exhausted","retryable":true}

PROBE-B  ready 实例存在但 current_prepare_intent_id 为空
  报告: status=degraded causes=['engine_receipt_expired'] instances=['missing','missing']
  创建: 429 job_capacity_exhausted

PROBE-C  实例 ready、回执 expiring（可用），pool_state='draining'
  报告: status=admitting causes=[] capacity={max:2,held:1,quar:0}
  创建: 429 job_capacity_exhausted
```

PROBE-C 是**决定性的**：报告 `admitting`，同一部署的每一次创建都 `429`，而且
`retryable: true` 会把客户端送进无限重试。`#845` 的原始形态（绿信号 + 创建被拒）在
新面上原样复现，只是原因从回执过期换成了池不可用。

位置：`backend/src/speechnexus_backend/speech/infrastructure/public_job_admission.py:216-219`
（无 ready 实例 → `job_capacity_exhausted`）与 `:634-637`（`pool_state != "active"` →
`job_capacity_exhausted`）。报告读的 `_READY_INSTANCE_QUERY`
（`backend/src/speechnexus_backend/speech/infrastructure/public_jobs_postgres.py:102-112`）
既不按 binding 过滤，也不读 `resource_pools.pool_state`，所以这两条创建侧的拒绝**结构上
不可能**出现在报告里。

## 4. 错误码双向比对

`createJob` 在 `contracts/speech-openapi.yaml:984-1130` 声明的码（逐状态枚举：
422 有 19 码、500 有 1 码、503 有 7 码；400/401/403/404/405/409/429 走共享
`ErrorResponse`，**没有码枚举**）。

### 4.1 正向：每个 cause 的「创建时对外回答」是否成立

| cause | 契约声称 | 实测/代码 | 结论 |
| --- | --- | --- | --- |
| `engine_receipt_expired` | 503 `engine_not_ready` | 回执过期：✓（集成用例实测）。**无 ready 实例：429 `job_capacity_exhausted`**。回执存在但不可解码：`engine_state_mismatch` | **部分错** |
| `capacity_exhausted` | 429 `job_capacity_exhausted` | ✓（集成用例实测，含 `Retry-After: 1`） | ✓ |
| `storage_unavailable` | 503 `job_unavailable`，`retryable:false` | ✓（集成用例实测，撤销 `workspace_quotas` SELECT） | ✓ |
| `workspace_not_provisioned` | 422 `workspace_not_configured` | ✓（集成用例实测） | ✓ |

`contracts/speech-openapi.yaml:1182` 的「or no ready instance is registered at all |
503 `engine_not_ready`」与
`docs/operations/engine-readiness-observability.md:44` 同处断言，都是错的。

### 4.2 反向：每个创建会返回的码是否都有 cause（这是真正的漏洞）

只列**部署态**（换请求也救不回来）的码；请求形状类的码按作者声明的边界排除。

| `createJob` 码 | 状态 | 有 cause？ | 备注 |
| --- | --- | --- | --- |
| `engine_not_ready` | 503 | ✓ `engine_receipt_expired` | |
| `job_capacity_exhausted` | 429 | ✓ `capacity_exhausted` | 但 PROBE-A/C 这类容量拒绝**没有** cause |
| `job_unavailable` | 503 | ✓ `storage_unavailable` | |
| `workspace_not_configured` | 422 | ✓ `workspace_not_provisioned` | |
| **`engine_state_mismatch`** | 503 | ✗ | `not_found`/`stale_instance_generation`/`reconciliation_required`；作者在契约 `:1205` 承认为「recorded gap」，但**没有 cause、没有测试、运维文档一字未提** |
| **`engine_resource_disabled`** | 503 | ✗ | `policy_or_resource_disabled` 包含**审批/配方授权窗口过期**（`admission.py:394-395`：`now_us >= approved.admission_expires_at_us`）——与回执过期**同一个时间窗形状**，是 #845 的直接邻居；`unapproved_runtime_template` 同族 |
| `recipe_not_admitted` | 503 | ✗（作者声明为请求形状） | 边界成立 |
| `job_admission_contended` | 503 | ✗（可重试竞态） | 合理 |
| `job_outcome_unknown` | 503 | ✗（单请求后果未知） | 合理 |
| `admission_state_invalid` | 500 | ✗（部署缺陷） | 合理 |
| `workspace_not_found` / `workspace_disabled` | 422 | ✗ | 会落到 `workspace_not_provisioned`：方向对（至少 degraded），**类名错**（运维被告知「去建配额」而实际是 workspace 不存在/被停用） |

**最需要作者回答的一条**：`engine_resource_disabled` 可以由
`approved.admission_expires_at_us` 过期触发（`backend/src/speechnexus_backend/speech/domain/admission.py:394-395`），
即「一个时间字段到了，所有建 Job 都 503，而所有健康面全绿」——与 #845 的**同一个机制**。
报告完全不看这个窗口，契约描述也没提。若不是本轮要改，至少要在契约描述与运维文档的
「不覆盖范围」里点名，并开一张卡。

### 4.3 一个测试口径问题：429 的「已声明」检查是空的

`tests/contract/test_job_admission_readiness.py:225-240` 的一半断言是
「`createJob` 为这个状态声明了这个码」，走 `response_contract("createJob", status)`。
但 429 走共享 `TooManyRequests` → `ErrorResponse`，`code` 只有
`^[a-z][a-z0-9_]{2,63}$` 的模式、**没有枚举**。实测：

```
_create_job_accepts(429, "job_capacity_exhausted")      → True
_create_job_accepts(429, "engine_not_ready")            → True    ← 错的码也通过
_create_job_accepts(429, "workspace_not_configured")    → True    ← 错的码也通过
_create_job_accepts(503, "job_capacity_exhausted")      → False   ← 503 有枚举，有牙
```

也就是说 `capacity_exhausted` 那一行只有状态断言有效（`job_error(code).status_code == 429`），
「契约声明了它」这一半恒真。不是缺陷，但不要让「一码一因」的措辞替这条断言背书。

## 5. `/readyz` 是不是真的零改动

- `git diff origin/main...HEAD -- contracts/operations.openapi.yaml` = **空**。✓
- `backend/src/speechnexus_backend/speech/application/readiness.py`、
  `interfaces/http/health.py`、`infrastructure/readiness.py` 均未出现在 30 文件里。✓
- `engine_readiness.py` 唯一的语义相关改动是把 `DEFAULT_WARNING_SECONDS = 21_600` 改成
  引用 `DEFAULT_READINESS_WARNING_SECONDS`（同值）。断言 `expiring` 的阈值不变；
  依赖方向是 CLI 模块 → domain，`lint-imports` 9 kept / 0 broken。
- `environment.py` 新增 `SPEECHNEXUS_ADMISSION_WATCH_` 前缀 plane：这是
  `tests/ci/test_service_environment_keys.py` 那条门禁**要求**的（compose 里新服务带这些键），
  不是放宽。注释里「Compose forwards them to that container」措辞略绕（API 容器并不收这些键），
  不影响语义。
- `job_runtime.py` 只多传了 `readiness=adapter`。✓
- #828 刚动过的 `engine_readiness.py` 的报告语义（`valid/expiring/expired/missing/invalid`
  与 renewal 提示）未被改。✓

**这一条作者做对了。**

## 6. 契约与生成物

- 生成物由生成器产出，实测：

  ```
  $ uv run python scripts/dev-env/generate-contract-bindings.py --check
  Found 36 errors (36 fixed, 0 remaining).
  Contract bindings: up to date        (exit 0, 之后 git status --porcelain 为空)
  ```

- 「+145 的 `file.schema.json` / +154 的 `file-workflow.json`」不是夹带：这两个文件是
  **自包含打包**（内联 speech 的 `$defs` 与 `x-…` 名称表），speech 侧新增一个 schema
  必然让它们同步增长，`file.generated.ts` 只多一行 `| "AdmissionReadiness"`。属生成器输出。
- `info.version` 保持 `0.1.0.dev18`：与最近 6 个动契约的合并一致（`eab6e129` 起
  dev17→dev18 后，`#690/#703/#702/#704/#828` 都没再 bump）。且返修版基线已含 #855，
  版本对耦合已解开（#855 的反例表里「只 bump `info.version` → 绿」），所以**不 bump 也不
  bump 都不会假红**。不是缺陷。
- `operationId: getJobAdmissionReadiness`、`bindings.json`、`source-inventory.json` 的 sha
  更新都是生成器产物。✓
- 路由字面段与 `{job_id}` 不冲突，理由成立：`Identifier` 模式
  `^[a-z0-9][a-z0-9._:-]{1,126}[a-z0-9]$`（`contracts/shared/identifiers.schema.json`）不含
  `-`，且 `jobs.py:874` 注册在 `get_job` 之前。✓

## 7. 变异：有牙，但一处弱化需要记录

单点施加、单独运行、每次复原（`git checkout --` 后 `git status --porcelain` 为空），
`git diff -U0 | grep -c '^@@'` 均为 1。

| # | 变异 | 结果 |
| --- | --- | --- |
| M1 | 删掉 `deploy/compose.yaml` 的整个 `job-admission-watch` 服务块 | **4 failed / 15 passed**（`ComposePollerTests` 四条） |
| M2 | `CAUSE_CODES[CAPACITY_EXHAUSTED]` → `STORAGE_UNAVAILABLE_CODE` | **1 failed / 12 passed**（`test_every_cause_names_a_create_code…[capacity_exhausted-429]`） |
| M3 | 删掉 `profiles: ["admission-watch"]` 这一行（退回 base） | **1 failed / 18 passed**（`test_the_poller_is_its_own_opt_in_profile`） |

M3 说明返修后那条护栏**换了方向且两个方向都有牙**，值得表扬。
M2 的红来自状态断言（`assert 503 == 429`），不是「码表已声明」那一半（见 4.3）。

**另需记录：为容纳新路由，`tests/unit/test_route_conformance.py` 放宽了 3 条断言**
（`+17/−1` 里的主体）：

- `test_a_probe_that_reaches_an_endpoint_fails_the_gate`：`assert invoked == [probe_path]`
  → `assert probe_path in invoked and all(... 前缀 ...)`——不再钉住「只派发一次」，重复派发会通过；
- `test_a_generator_endpoint_is_counted_and_fails_the_gate`：`ran == ["generator"]`
  → `ran and set(ran) == {"generator"}`——同上；
- `test_lost_evaluate_evidence_fails_cli_with_failed_rows`：硬编码 `42` → 从契约派生的
  `declares_405`（**这一条是改进**，不是放宽）。

前两条是「新增一条同族路由导致 catch-all 多派发一次，于是把精确断言换成集合断言」。
方向可理解，但**门禁精度实际下降**了；更严谨的写法是断言新的精确集合。不阻断，但请在
PR 里承认这是放宽而不是等价改写。

## 8. 实跑（`555f6290`，macOS arm64，Python 3.14.9，Node 26.12.1）

| 检查 | 结果 |
| --- | --- |
| `pytest tests/ci -q` | **433 passed, 1418 subtests passed** |
| `pytest tests/contract -q` | **3900 passed, 0 failed** |
| `pytest tests/unit/backend -q` | **3364 passed, 6 subtests passed** |
| `pytest tests/integration/test_job_admission_readiness_pg.py`（隔离 PG 16.15 + MySQL 8.4.8 容器，用后删除） | **5 passed** |
| `docker compose --env-file deploy/compose.env.example config --quiet` | base + 11 个 profile（`admission-watch`/`artifact-retention`/`asset-inspection`/`binding-disable`/`engine-readiness`/`engine-registration`/`job-dispatch`/`migration`/`web`/`workbench-api`）**全部 exit 0** |
| `generate-contract-bindings.py --check` | exit 0，`git status` 干净 |
| `ruff check .` / `ruff format --check .` | All checks passed / 1921 files already formatted |
| `lint-imports` | 9 kept, 0 broken |

对比派单给的基线：主干 `c7e43f9a` 那条 `assert 0 == 27` 的红在返修版基线里已消失
（基线前进到含 #855、#856 的 `45db3910`），本轮 0 failed 与它无关、也不掩盖它。

**环境注记（不是本 PR 的问题）**：`uv sync --locked` 与 `make install` 在当前主干上即失败
（`uv.lock` 落后于 `backend/pyproject.toml`），在 `origin/main` 上同样复现；本轮用
`uv sync --frozen` + 仓库外 `UV_PROJECT_ENVIRONMENT` 绕开，未改 `uv.lock`。
`deploy/README.md`、`docs/operations/README.md` 的更新不在本 PR 内（见 2.2）。

## 9. 改动面：30 文件 / +3747 −20 是否都必要

| 类别 | 文件 | 判断 |
| --- | --- | --- |
| 产品代码 | `job_admission_readiness.py`(新 357)、`admission_watch.py`(新 359)、`jobs.py`、`public_jobs.py`、`public_jobs_postgres.py`、`job_runtime.py`、`engine_readiness.py`、`environment.py` | 必要；无夹带 |
| 契约 | `speech-openapi.yaml`(+203)、`bindings.json` | 必要 |
| 生成物 | `file.schema.json`、`file.generated.ts`、`speech.generated.ts`、`models.py`、`parsers.py`、`file.validators.generated.mjs`、`source-inventory.json`、`tests/fixtures/contracts/file-workflow.json` | 生成器产物（第 6 节实测） |
| 测试 | 4 个新文件（1097 行）+ 3 个既有文件小改 | 必要 |
| 部署/文档 | `compose.yaml`、`compose.env.example`、新运维文档 | 必要；**`deploy/README.md` 缺改（2.2）** |

未发现与本卡无关的产品代码改动。`+3626`→`+3747` 的增量全部来自返修（profile 化 + 验收护栏
+ 文档）。**没有夹带独立的业务改动。**

## 10. 必须修清单

**阻断（B，合入前必须解决）**

1. **B1**：池不是 `active`（`pool_state='draining'` 等）时报告 `admitting` 而创建 `429`。
   要么报告读 `resource_pools`/`pool_owner` 并新增 cause（例如 `capacity_exhausted` 的
   扩容判据），要么在契约与运维文档里把「池状态不覆盖」写成显式边界。
   （实测 PROBE-C；`public_job_admission.py:634-637` vs `public_jobs_postgres.py:102-112`）
2. **B2**：`contracts/speech-openapi.yaml:1182` 与
   `docs/operations/engine-readiness-observability.md:44` 关于「没有 ready 实例 → 503
   `engine_not_ready`」的断言与实现相反（实际 429 `job_capacity_exhausted`）。
   `_engine_detail` 末句「Admission answers engine_not_ready」同样要改。
   （实测 PROBE-A/PROBE-B）
3. **B3**：`engine_state_mismatch`、`engine_resource_disabled` 两个持久态没有 cause。
   至少：在契约的「不覆盖范围」里点名这两个码（现在只提了 `recipe_not_admitted`/binding/制品），
   在运维文档同一处同步；并为 `admission_expires_at_us` 过期（
   `domain/admission.py:394-395`）单独开卡——它与 #845 同机制。

**必须修（M，可与 B 同轮）**

4. `docs/operations/engine-readiness-observability.md:113` 的「healthcheck 会因为状态文件
   过期而变红」对已退出容器不成立，与本文件 140 行矛盾；改为「容器消失，需
   `docker compose ps -a` / 日志流才能发现」。
5. `docs/operations/engine-readiness-observability.md:36`、`:169` 的「三类」应为「四类」。
6. 新运维文档是孤儿：在 `docs/operations/README.md` 与 `docs/README.md` 索引里加入口
   （`deploy/README.md` 的因素材依赖可继续留给 #823，但请在 PR 描述里明确这是**唯一的**
   运维主入口缺口）。
7. 轮询者进 base 的旧版已撤；`profiles` 版本请保留，并在 PR 描述里不要再引用
   「base 含 job-admission-watch」的旧实测命令。

**建议（S）**

8. `start_period` 从 5s 提到 ≥120s，或放宽 `depends_on` 禁令到「不得 `service_healthy`
   依赖观测面」（2.6）。
9. `tests/unit/test_route_conformance.py` 的两处集合化断言改回精确集合，或在 PR 里
   显式承认门禁精度下降（7）。
10. 报告的四类 cause 与 `createJob` 码之间加一条**反向**守卫：枚举 `_CREATE_REFUSAL_CODES`
    的部署态子集，逐个要求「有 cause 或明确列在不覆盖清单里」。当前双向比对的另一半
    （4.2）完全无测试。

## 11. 只能依赖作者陈述的点

以下我**没有**独立验证，报告中按作者陈述采信，并在此明示：

- **镜像里是否真的装上了 `speechnexus-admission-watch` console script**：我没有执行
  `docker build` / `make python-build`（构建成本与本地环境不符）。我验证的只是
  `backend/pyproject.toml` 的入口声明存在、本机 venv 里 `uv sync` 会装出该入口、
  以及 `build: *python-build` 与 `api` 同一个 Dockerfile。真实镜像内入口、以及
  `--healthcheck` 在镜像的只读根 + tmpfs 下能否写状态文件，属作者陈述。
- **真实容器上的 `docker ps: Up (healthy) → Up (unhealthy) → Up (healthy)` 与
  `RestartCount=0`**：PR 正文称已实测。我没有从 `deploy/compose.yaml` 起这套栈
  （需要 API/DB/MinIO/私有配方），只做了 `config --quiet` 渲染验证。
- **回执 24 小时自然过期**：集成用例用「就地重写回执时间戳」构造状态（作者在模块 docstring
  里如实标注），我也没跑真实时钟跨度。
- **对象存储探针**：集成用例里是 `StaticProbe("object_storage")` 打桩，不参与。
- **`engine_state_mismatch` 的持久可达性**：4.2 的两条是基于代码路径（`PUBLIC_ADMISSION_CODES`
  映射 + `require_live_authority`）的推断，我没有构造出「报告 admitting、创建
  `engine_state_mismatch`」的 PG 探针（B1/B2 已用探针实测，B3 的
  `engine_resource_disabled` 分支依赖私有配方文件里的 `admission_expires_at_us`，无法在不改
  产品代码的前提下构造）。

---

审查执行环境：worktree `/private/tmp/sn859`，分支 `review/859-845`（基于
`origin/main` = `45db3910`），被审 head `555f6290`（另核 `bc7c4dc3`，已 orphaned）。
隔离容器 `sn859-pg`（postgres:16.15-alpine）、`sn859-mysql`（mysql 8.4.8）在集成测试后删除，
未触碰同机其它评审容器与引擎容器。

---

# 第二轮审查（head `6e4d5b8a`）

审查对象：PR [#857](https://github.com/hrygo/SpeechNexus/pull/857) 的返修 head
`6e4d5b8a`（`fix/845-admission-observability`）。分支 `review/861-845-v2`，基线
`origin/main` = `2cc0efa5`。被审分支的合并基是 `45db3910`，**不含**本轮报告所在的
`2cc0efa5`（#860 合并了第一轮报告），所以 `git diff origin/main...HEAD` 显示的是本 PR
的全部 34 个文件，而不是只有报告。

本轮报告单独成一个 PR，**只改本文件**。

## R0. 结论

**不能合。** 第一轮的三条阻断里有一条（B3）真正关闭，一条（B2）关闭，一条（B1）只关了一半；
同时返修新引入两个**会直接把 CI 打红**的问题，以及一处与第一轮同类的装载层缺口：

| 编号 | 事实 | 性质 |
| --- | --- | --- |
| **N1** | `uv run mypy` **4 个错误**，全部落在本 PR 新增的 `deployment_refusals` 里（`domain/admission.py:477/483/484/489`，`Cannot infer type of lambda`）。`ci.yml:376` 就是这个门禁。 | **CI 红** |
| **N2** | `tests/integration/test_job_admission_readiness_pg.py` **1 failed**：第 326 行断言 `["workspace_not_provisioned"]`，产品实际产出 `workspace_not_configured`（cause 已改名，测试没跟）。作者自述「本轮没跑成」，实测确实没跑成。 | **CI 红** |
| **N3** | 装载层仍缺一项部署态：`AdmissionReadinessFacts` 与 `deployment_refusals` **都不含 hosts**。`host_nodes.node_state` 非 `active` 时，创建 `503 engine_resource_disabled`，报告 **`admitting`**。真库实测。 | **与第一轮 B1 同类，未闭合** |
| **N4** | 创建路径在**准入之前**还有一层拒绝：`public_job_admission.py:634-637` 把 `pool_state != 'active'` 直接答成 **429 `job_capacity_exhausted`**；报告把它映射成 `engine_resource_disabled`（503）。真库实测：报告说 `engine_resource_disabled`，`POST` 答 429。第一轮 B1 的「状态」半边修好了，「码」半边没修好——而且方向反了：报告现在点名一个创建永远不会返回的码。 | **阻断** |
| **N5** | `causes` 多值顺序与创建短路顺序不一致，而 `detail` 用 `causes[0]` 断言单一码。两处同时命中时，`detail` 会写一句假话。真库 + 单测双证。 | **阻断** |
| **N6** | `_classify_chains` 用 `min(chain, key=(len, chain))` 选「创建会走的那条链」。多一个**无法解码的幽灵实例**（长度 1 的链）就能压过真实实例长度 2 的链，把报告的 cause 从 `capacity_exhausted` 翻成 `engine_state_mismatch`。真库 + 单测双证。 | **阻断** |

N3/N5/N6 都是「报告与创建回答不同问题」——本 PR 要消灭的那类缺陷，只是换了位置。

## R1. 审查环境与执行事实

| 项 | 值 |
| --- | --- |
| worktree | `/private/tmp/sn861`，分支 `review/861-845-v2` |
| 被审 head | `6e4d5b8a`（detached 检出后比对；返修报告分支基于 `origin/main` = `2cc0efa5`） |
| Python | 3.14（worktree 内 `UV_NO_CONFIG=1 uv sync --locked --all-packages --all-groups`，`EXIT=0`） |
| Node / pnpm | v26.12.1 / 12.14.0（`pnpm install --frozen-lockfile`，`EXIT=0`） |
| 隔离 PostgreSQL | 自建 `sn861-pg`（`postgres:16.15-alpine`，`127.0.0.1:55471`），用后 `docker rm -f` |
| 隔离 MySQL | 项目既有 `speechnexus-coordination-mysql`（`127.0.0.1:55434`，未改动其状态） |
| 未触碰 | 同机其它评审容器（`sn824-*`/`sn838-pg`/`sn832-*`）与共享 GPU 引擎容器 |

`~/.config/uv/uv.toml` 的清华镜像问题用 `UV_NO_CONFIG=1` 绕开，**不作为失败项**。

## R2. 装载层：第一轮的关键缺口有没有被证明

### R2.1 真库实验（重做第一轮的 draining 实验，并扩到其它拒绝态）

构造方式：在隔离 PG 上装好完整协调 schema + 生产 HTTP 组合根
（`create_app(...)` + `ASGITransport`），**同一个进程内**先 `GET .../admission-readiness`
再 `POST /v1/nexus/jobs`。探针文件用后即删，`git status --porcelain` 为空。

```
### pool_state=draining
  REPORT  status=degraded   causes=['engine_resource_disabled']
          detail: A ready engine instance, its pool or the approved recipe is not active,
                  so a create answers engine_resource_disabled unt...
  CREATE  http=429  code=job_capacity_exhausted  retryable=True

### host node_state=quarantined
  REPORT  status=admitting  causes=[]
          detail: None
  CREATE  http=503  code=engine_resource_disabled  retryable=False

### owner_state=reconciling_takeover
  REPORT  status=degraded   causes=['engine_state_mismatch']
  CREATE  http=503  code=engine_state_mismatch  retryable=False

### control: healthy
  REPORT  status=admitting  causes=[]
  CREATE  http=202  code=None
```

读法：

- **owner 租约**：报告与创建一致（`engine_state_mismatch` / 503）。↔ 这一项做对了。
- **`pool_state='draining'`**：报告 `engine_resource_disabled`（=503 语义），创建 **429
  `job_capacity_exhausted`**。状态对了，**码错了**，而 `detail` 那句「a create answers
  engine_resource_disabled」是**假话**。这正是第一轮 PROBE-C 的那台部署。
- **`node_state='quarantined'`**：报告 **`admitting`**，创建 **503**。**假绿**，与第一轮
  B1 同一形态。

### R2.2 为什么 `node_state` 结构上不可能出现在报告里

- `AdmissionReadinessFacts`（`domain/job_admission_readiness.py`）字段是
  `workspace_id / workspace_state / quota / pools / runtimes / undecodable / now_us`——
  **没有 hosts**。
- `deployment_refusals(*, pools, runtimes, quota, now_us)`——**没有 hosts**。
- 装载器 `PostgresPublicJobs.admission_readiness_facts` 一次 SQL 都不读 `host_nodes`：
  `grep -c "host_nodes" backend/src/speechnexus_backend/speech/infrastructure/public_jobs_postgres.py` = **0**。
- 创建路径却读：`domain/admission.py:597` `if host.node_state != "active": raise policy_or_resource_disabled`。
- 三个 readiness 测试文件里 `grep -rn node_state` = **0 命中**，即这条路径零覆盖。

`node_state` 不是「请求形状」，也不是契约里已声明的边界。契约只写了
「Host-level resource **headroom** is not evaluated」（向量来自请求），**没有**写
`node_state` 不覆盖。所以这是缺口，不是已声明的边界。

### R2.3 作者自己那条真库测试在本 head 上是红的

```
$ .venv/bin/python -m pytest tests/integration/test_job_admission_readiness_pg.py -q
FAILED ...::test_a_workspace_without_a_quota_row_is_its_own_class[postgresql]
E   AssertionError: assert ['workspace_not_configured'] == ['workspace_not_provisioned']
1 failed, 4 passed in 3.54s
```

4 条通过的是：`admitting` 基线、回执过期、配额占满、撤销 `workspace_quotas` SELECT
（`storage_unavailable`）。**这三类装载确实通了**——所以返修不是无功；但
`pool_state` / `node_state` / 实例服务态这三类，装载与断言都缺。

## R3. 单一判据是不是真的单一

### R3.1 共享是真的，但只共享了「三层里的后两层」

返修把规则抽成 `require_quota / require_pool_live / require_runtime_live /
require_runtime_capacity / require_runtime_service`，报告用 `deployment_refusals` 收集，
创建用同批函数。**这一层是真的单一**，我用双向变异验证过（见 R6 的 M-b1/M-b2：删报告侧
红 2 条，删创建侧红 5 条）。

但创建路径实际有**三层**拒绝，报告只镜像了后两层：

| 层 | 位置 | 例子 | 报告覆盖 |
| --- | --- | --- | --- |
| L1 选择/预检 | `infrastructure/public_job_admission.py:634-637` | `pool_state != 'active'` → **429** | ✗ |
| L2 `require_live_authority` | `domain/admission.py` | 池状态、owner 租约、回执窗口 | ✓ |
| L3 `decide_admission` | `domain/admission.py` | host 状态、实例服务态、容量、配额 | 部分（缺 host） |

L1 的存在让 R2.1 的 draining 两行对不上；作者的单测
`test_a_draining_pool_is_reported_instead_of_admitting` 断言
`_create_code(facts) == "engine_resource_disabled"`，而它的 `_create_code` 只走 L2+L3
——**它给创建路径建了一个不含 L1 的模型，于是"等价"是在错误模型上证明的**。真库上
同一状态创建答 429。

### R3.2 多 cause 的顺序：`deployment_refusals` 顺序 ≠ 创建顺序，且 `detail` 又按字母序取第一个

两条路径的先后不同：

- **创建**：L2（池 → 全部 runtime 的 live）→ 配额版本 → `require_quota` →
  每 stage 的 host 状态 → `require_runtime_service` → **最后**容量。
- **`deployment_refusals`**：`require_quota` **最先** → 各池 → 各 runtime 的
  live → **capacity** → service。

即两处倒序：**配额 vs 池**，**容量 vs 服务态**。更关键的是 `_causes_from` 最后
`return tuple(sorted(causes))`（`job_admission_readiness.py:319`）——按**字母序**重排，
而 `_detail` 用 `causes[0]` 去断言一个具体码。

真库实测（同进程）：

```
### 回执过期 + 配额占满（两类同时命中）
  REPORT  status=degraded causes=['capacity_exhausted', 'engine_receipt_expired']
          detail: The workspace quota holds every attempt (held 4 + quarantined 0 of 4);
                  a create answers job_capacity_exhausted ...
  CREATE  http=503  code=engine_not_ready  retryable=False
```

单测层面同样可复现（手搭 facts，不涉及真库）：

```
A. 回执过期，配额正常
   status=degraded causes=['engine_receipt_expired']
   detail=... a create answers engine_not_ready
B. 回执过期 + 配额占满
   status=degraded causes=['capacity_exhausted', 'engine_receipt_expired']
   detail=The workspace quota holds every attempt ... a create answers job_capacity_exhausted
   # 创建实际答 503 engine_not_ready（配额在 require_live_authority 之后才检查）
```

契约自己写了这条边界：

> `causes` are evaluated independently and reported together ... a create answers
> whichever refusal it reaches first, and this surface names the classes rather than
> mirroring one request's precedence.
> （`contracts/speech-openapi.yaml`，`AdmissionReadiness` 描述）

**这句话与实现矛盾**：`causes` 确实多值（符合），但 `detail` 用 `causes[0]` 断言
「a create answers <具体码>」（不符合）。要么 `detail` 不再点名单一码，要么按创建优先级
排序。当前是「声明按类、文案按序」，两头都不成立。

### R3.3 还有一处：`min(chains, key=(len, chain))` 会选一条创建不会走的链

`_classify_chains`（`job_admission_readiness.py:308`）用「拒绝最少、再按码字母序」挑
「创建会走的那条链」。`undecodable` 实例被塞成一条长度 1 的 `reconciliation_required` 链。
于是：真实实例一旦有两类拒绝（长度 2），一条**无关的幽灵实例**就能把报告的 cause 抢走。

单测（同一真实实例，仅追加一个 undecodable）：

```
A. 回执过期，配额正常          -> causes=['engine_receipt_expired']
B. 回执过期 + 配额占满          -> causes=['capacity_exhausted','engine_receipt_expired']
C. B + 一个 undecodable 幽灵    -> causes=['engine_state_mismatch']   ← 翻面
D. 回执正常 + 配额占满 + 幽灵    -> causes=['capacity_exhausted']
```

C 是**非单调**的：什么都没变，只多了一个创建根本不会钉住的坏实例，报告从「配额/回执」
变成「实例状态不符」。

真库里这就是我实测到的那一格：fixture 有 `instance-new`（有回执）与 `instance-test`
（无 prepare intent → `undecodable`）；创建按 `ORDER BY instance_id LIMIT 1` 钉
`instance-new`，报告却描述了 `instance-test` 的链：

```
### 回执过期 + 配额占满（真库）
  REPORT  causes=['engine_state_mismatch']      ← 幽灵实例抢走了
  CREATE  http=503 code=engine_not_ready        ← 创建钉的是 instance-new
```

（`GET .../admission-readiness` 的 `instances[]` 只列 `instance-new`，因为
`undecodable` 的不进 `instances`——这本身也说明报告知道它不可用，却拿它当结论。）

## R4. 8 类 cause 的双向比对（我自己重做）

从 `contracts/speech-openapi.yaml` 的 `createJob` 逐状态枚举提取（不是手抄）：

```
422: asset_not_ready, job_binding_incomplete, job_kind_not_supported, job_request_expired,
     job_text_asset_unsupported, model_binding_mismatch, model_binding_not_current,
     model_binding_not_published, model_binding_retired, model_binding_task_not_supported,
     model_not_published, profile_not_published, pronunciation_not_found,
     unsupported_parameter, voice_draft_not_candidate, voice_not_published,
     workspace_disabled, workspace_not_configured, workspace_not_found
429: job_capacity_exhausted                      ← 内联枚举（第一轮 4.3 的空断言已修）
500: admission_state_invalid
503: engine_not_ready, engine_resource_disabled, engine_state_mismatch,
     job_admission_contended, job_outcome_unknown, job_unavailable, recipe_not_admitted
```

**正向（cause → 它点名的码 → 该码是否在该状态被声明）：8/8 成立。**

```
engine_receipt_expired   -> engine_not_ready          503 declared=True
engine_state_mismatch    -> engine_state_mismatch     503 declared=True
engine_resource_disabled -> engine_resource_disabled  503 declared=True
capacity_exhausted       -> job_capacity_exhausted    429 declared=True
workspace_not_found      -> workspace_not_found       422 declared=True
workspace_disabled       -> workspace_disabled        422 declared=True
workspace_not_configured -> workspace_not_configured  422 declared=True
storage_unavailable      -> job_unavailable           503 declared=True
```

**反向（有 cause 的码 vs 没有的）：** 无 cause 的 5xx 是 `admission_state_invalid`(500)、
`job_admission_contended`、`job_outcome_unknown`、`recipe_not_admitted`——第一轮已判为
「缺陷/竞态/请求形状」，可接受。

**集合相等断言成立：** `DEPLOYMENT_STATE_CODES == set(CAUSE_CODES.values())` → `True`，
两侧差集均为空。第一轮 B3 点名的 `engine_state_mismatch`、`engine_resource_disabled`、
`workspace_not_found`、`workspace_disabled` **都补上了**，`workspace_not_*` 三种也拆开了。
↔ **B3 关闭**。

429 那半边也确实有牙了：

```
_create_job_accepts(429, 'job_capacity_exhausted')  = True
_create_job_accepts(429, 'engine_not_ready')        = False
_create_job_accepts(429, 'engine_state_mismatch')   = False
_create_job_accepts(503, 'job_capacity_exhausted')  = False
```

一条**保留意见**（不阻断）：`DEPLOYMENT_STATE_CODES` 是 `job_errors.py` 里**手工维护的
第二个清单**，不是从 `createJob` 的契约枚举派生的。所以
`test_the_causes_are_exactly_the_declared_deployment_state_answers` 比的是**两个 Python
常量**；往契约的 503 枚举里加一个新码而不同步这个常量，测试**不会**红。它比第一轮强
很多，但还不是注释里说的「与 createJob 声明的码集合双向闭」——建议派生自契约枚举。

## R5. 上一轮各项回归

| 上轮项 | 本轮实测 | 判定 |
| --- | --- | --- |
| **B1** 池非 active → 报告 admitting | 报告不再 admitting；但创建答 429 而报告点名 503 `engine_resource_disabled` | **半关**（见 N4） |
| **B2** 无 ready 实例的码与文案 | cause 改 `capacity_exhausted`；契约该行改为「no ready engine instance ... 429」；`_detail` 同步 | ✓ **关闭** |
| **B3** 两个 503 码无 cause | 8 类补齐，集合相等断言成立 | ✓ **关闭** |
| **M4** 文档「healthcheck 会变红」 | 第 113 行已改为「Docker 不再评估已退出容器的 healthcheck，既不会变红也不会 `unhealthy`」 | ✓ |
| **M5** 「三类/四类」计数 | 全文件已无「三类/四类」字样，改为「闭合枚举」 | ✓ |
| **M6** 孤儿文档 | `deploy/README.md:50` 已挂链；但 `docs/README.md` / `docs/operations/README.md` 仍 **0 命中** | **半关** |
| **M7/S9** route conformance 精确断言 | `sorted(invoked) == sorted([probe_path, ".../admission-readiness"])`、`ran == ["generator","generator"]` 均为精确相等；`42` 改为契约派生 | ✓ |
| **S8** `start_period` ≥120s | 仍是 `start_period: 5s` | 未采纳（本就只是建议） |
| **S10** 反向守卫 | 见 R4 保留意见 | 部分 |

**另外**：文档 cause 表第 46 行现在写「`engine_resource_disabled` … `503
engine_resource_disabled`」，把 `pool_state` 也归进这一行——**真库证伪**（draining 是
429）。这是本轮新引入的文档事实错误，与 N4 同源。

残留的同名漂移：`workspace_not_provisioned` 这个**已被改名的** cause 仍留在
`contracts/speech-openapi.yaml:1234`、`:6682`、生成物
`packages/contracts/src/speechnexus_contracts/file.schema.json:3858`、
`deploy/compose.yaml:734`、`tests/contract/test_job_admission_readiness.py:115` 注释，
以及 N2 的失败断言里。冻结契约里描述了一个枚举中**不存在**的 cause。

## R6. 变异（我自己重做，单点施加、跑完复原）

每次 `git checkout --` 后 `git status --porcelain` 为空。

| # | 变异 | 结果 |
| --- | --- | --- |
| M-a | 删掉 `jobs.py` 的 `@router.get(PREFIX + "/admission-readiness", ...)` 整块 | **3 failed, 59 passed**（`test_repository_has_no_unbaselined_deviation`、`test_partial_405_probe_mapping_fails_only_the_missing_operation`、`test_lost_evaluate_evidence_fails_cli_with_failed_rows[none]`） |
| M-b1 | `deployment_refusals` 里删掉 per-runtime 的 `require_runtime_service`（只删报告侧） | **2 failed**（`[instance_stopped]`、`[undecodable_instance]`） |
| M-b2 | `decide_admission` 里删掉 `require_runtime_service`（只删创建侧） | **5 failed**（`test_job_admission_readiness` 2 + `test_admission` 3） |
| M-c | `CAUSE_BY_REFUSAL_CODE["capacity_exhausted"]` 改成 `ENGINE_RECEIPT_EXPIRED` | **4 failed** |
| M-d | 删掉 `job-admission-watch` 的 `profiles: ["admission-watch"]` | **1 failed**（`test_the_poller_is_its_own_opt_in_profile`），且 base `config --services` 变成含 `job-admission-watch` |
| — | 复原后 base `config --services` | `api gateway minio minio-init postgres`（与第一轮基线一致）✓ |

M-b1/M-b2 说明「共享谓词」两个方向都有牙——这是本轮**做得最实**的一处。
M-a 我测到的是 **3 条**，不是作者说的「4 条红」；数量对不上，但门禁确实有牙。

## R7. 实跑（`6e4d5b8a`，macOS arm64，Python 3.14，Node 26.12.1）

| 检查 | 结果 |
| --- | --- |
| `pytest tests/ci -q` | **433 passed, 1418 subtests passed** |
| `pytest tests/contract -q` | **3903 passed** |
| `pytest tests/unit/backend -q` | **3370 passed, 6 subtests passed** |
| `pytest tests/integration/test_job_admission_readiness_pg.py`（隔离 PG 16.15 + MySQL 8.4.8） | **1 failed, 4 passed** ← N2 |
| `uv run mypy` | **4 errors**（`domain/admission.py:477/483/484/489`）← N1 |
| `ruff check .` / `ruff format --check .` | All checks passed / 1921 files already formatted |
| `uv run lint-imports` | **9 kept, 0 broken** |
| `pnpm typecheck` / `pnpm lint` | exit 0 / ESLint passed |
| `docker compose --env-file deploy/compose.env.example ... config --quiet` | base + 10 个 profile **全部 exit 0** |

环境注记：第一次跑 `tests/contract` 有 8 条 `ERR_MODULE_NOT_FOUND: openapi-fetch` 失败，
原因是该 worktree 未装 Node 依赖（我只跑了 `uv sync`）；`pnpm install --frozen-lockfile`
后重跑 **3903 passed**。**这不是本 PR 的问题**，已排除。

## R8. 必须修

**阻断（合入前必须解决）**

1. **N1 mypy 4 红**：`_refused_by(lambda ...)` 的 `Callable[[], None]` 无法推断带默认参数的
   lambda 类型。用 `functools.partial` 或显式 `def` 包一层。`ci.yml:376` 会红。
2. **N2 集成测试红**：`tests/integration/test_job_admission_readiness_pg.py:326` 的
   `workspace_not_provisioned` 改 `workspace_not_configured`；同时清掉 R5 末尾列出的
   契约/生成物/compose/注释里的同名漂移。**请在提交说明里给出这条测试的实跑输出**——
   本轮它没跑过。
3. **N3 装载层补 host**：`AdmissionReadinessFacts` 与 `deployment_refusals` 加 `hosts`，
   装载器读 `host_nodes`，并让 `require_runtime_service` 承接 `node_state` 判定（与
   `decide_admission:597` 同一处）。加一条真库用例：`host_nodes.node_state='quarantined'`
   时报告不得 `admitting`。
4. **N4 draining 的码对不上**：二选一并写进契约——
   (a) 报告对 `pool_state` 走 L1 的答案（`capacity_exhausted`），或
   (b) 把 L1 的 `pool_state` 判定挪进共享谓词，让创建也答 `engine_resource_disabled`。
   现状是报告点名一个创建不会返回的码，且文档第 46 行按错的写。
5. **N5 多 cause 与 `detail`**：`detail` 不要再从 `causes[0]` 断言单一码（改为按类描述），
   或者按创建优先级排序并去掉字母序 `sorted()`。二者取一，并让契约措辞与实现一致。
6. **N6 链选择**：`min(chains, key=(len, chain))` 改成「创建真正会钉住的那条链」（按
   `_instance` 的选择规则），或至少让 `undecodable` 不参与「最不坏链」的竞争（它应该是
   加权重，不是更短的链）。

**必须修（可与上同轮）**

7. 文档第 46 行删掉 `pool_state` 归入 `engine_resource_disabled` 的说法（跟随 N4 的结论）。
8. 孤儿文档：`docs/README.md` 与 `docs/operations/README.md` 补索引行（第一轮 M6 未全做）。
9. `DEPLOYMENT_STATE_CODES` 改为从 `createJob` 契约枚举派生（R4 保留意见）。
10. 更新 PR 说明：本轮的变异实测是 3 条红，不是 4 条。

**建议（S）**

11. `start_period: 5s` → ≥120s（第一轮 S8，仍未采纳，冷启动噪声仍在）。
12. 给 `deployment_refusals` 补一条「顺序 = 创建顺序」的守卫测试，否则 N5 会复发。

## R9. 未验证 / 只能依赖作者的点

- **真实镜像内 `speechnexus-admission-watch` 入口与只读根 + tmpfs 写状态文件**：未执行
  `docker build` / `make python-build`，与第一轮相同。
- **`docker ps` 实际状态迁移与 `RestartCount`**：未起整套栈，只做 `config --quiet`。
- **回执自然 24h 过期**：与第一轮相同，用就地重写构造。
- **我构造的 `workspace_quotas`/`resource_pools`/`host_nodes` 行**：都是对真实 schema 的
  `UPDATE`（不是新建表），但「运维会把池置为 draining / 主机会被 quarantine」的**触发路径**
  我读到了代码（`domain/leases.py:215-218` 在超阈值时把 `node_state` 置 `quarantined`；
  `engine_registration.py:387-397` 恢复），**没有**跑一次真实的租约隔离来产生它。
- **N2 之外是否还有未跑的测试**：我只跑了 CI 覆盖的四个套件 + 该集成文件；本机没有
  `SPEECHNEXUS_TEST_MINIO_*`，`make integration-test` 的全量门禁未跑。
- **`check-doc-refs.py` 的 `MISSING`**：本报告按第一轮同样体例用 `路径:行号` 引用**被审分支**的
  文件（`docs/operations/engine-readiness-observability.md`、`admission_watch.py` 等），这些文件
  只存在于 PR 分支，在本报告所在的分支上自然解析不到，`MISSING` 从 43 升到 47 全部来自此处。
  该脚本明示为本地报告工具、不接入 CI、失败不阻止合并（`docs/validation/ci-validation.md:546`）。

---

第二轮审查环境：worktree `/private/tmp/sn861`，分支 `review/861-845-v2`（基于
`origin/main` = `2cc0efa5`），被审 head `6e4d5b8a`。自建 `sn861-pg`
（`postgres:16.15-alpine`，`127.0.0.1:55471`）用完即删；MySQL 复用项目既有的
`speechnexus-coordination-mysql`（未改其状态）。所有变异施加后均已 `git checkout --` 复原，
`git status --porcelain` 为空。未触碰任何共享 GPU 引擎容器。
