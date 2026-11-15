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
