# Job admission observability

「创建 Job 是否可准入」现在是一个**可轮询的信号**，不再需要发一次创建请求才能看到拒绝。

`POST /v1/nexus/jobs` 在引擎 readiness 回执滑出有效期后返回 `503 engine_not_ready`，而
`/readyz`、容器 healthcheck 和 `docker ps` 全部保持绿色。原因是它们回答的不是同一个问题：
`/readyz` 回答“这个进程还能不能服务流量”，准入回答“这一次创建会不会被拒”。把准入塞进
`/readyz` 会让一次只影响创建的事件把整个 API 从负载均衡摘掉（#828 review B8 的结论，
shared `contracts/operations.openapi.yaml` 也因此不改）。

本文档描述这条信号的两个面：**报告**（API 上的只读路由）和**轮询者**（参考部署里真正在读它的容器）。

## 1. 报告：`GET /v1/nexus/jobs/admission-readiness`

| 项目 | 事实 |
| --- | --- |
| 路由 | `GET /v1/nexus/jobs/admission-readiness`（`operationId: getJobAdmissionReadiness`） |
| 契约 | `contracts/speech-openapi.yaml` 的 `AdmissionReadiness`，随 `make contracts-generate` 生成 Python/TS 类型 |
| HTTP 状态 | 只要 Job 面装配好就是 **`200`**；判定在 body 里。未装配 Job 面时与其它 Job 路由一致返回 `503 job_unavailable` |
| 语义 | `status` 只有 `admitting` / `degraded`；`causes` 是空或闭合枚举 |
| 副作用 | 无。只读事务、不加锁、不写行、不接触引擎 |
| 缓存 | `Cache-Control: no-store` |
| 作用域 | `?workspace_id=`（默认 `workspace-default`）。报告读的是该 workspace 的配额行与实例回执，传错 workspace 得到的是另一个 workspace 的真话 |

`status` 与 `causes` 的关系是硬约束：`causes` 为空 **当且仅当** `status: admitting`。
`400 invalid_job_request` 只表示 `workspace_id` 不是合法标识符，与部署状态无关。

### 三类机器可读原因

`causes[]` 的成员就是机器可读的类，不要解析 `detail` 里的句子。

| `cause` | 含义 | 创建时的对外回答 | 处置 |
| --- | --- | --- | --- |
| `engine_receipt_expired` | 没有任何 ready 实例持有可用 readiness 回执：已过期、缺失、无法解码，或根本没有注册 ready 实例 | `503 engine_not_ready`（完全不可用的回执为 `503 engine_state_mismatch`） | `speechnexus-register-engine --renew-readiness`；没有回执时重新注册 |
| `capacity_exhausted` | workspace 配额已占满，或所有可用回执的实例 slot 已满 | `429 job_capacity_exhausted` 带 `Retry-After` | 等待 attempt/slot 释放；不是存储故障 |
| `storage_unavailable` | 在准入身份上读不到协调库，因此其它事实也读不到；**单独出现** | `503 job_unavailable`，`retryable: false` | 恢复数据库读权限/连接 |
| `workspace_not_provisioned` | 该 workspace 没有配额行，准入无处记账 | `422 workspace_not_configured` | 为该 workspace 注册引擎以写入配额行 |

`causes` 之间**独立判定、可同时出现**，按枚举定义顺序排列：一个既过期又满载的部署会同时报出两类，
而不是按某一次请求的判定顺序只报一个。`expiring`（仍在预警窗口内但尚未过期）**不是** cause，
它仍然可以准入。

报告不覆盖**请求相关**的拒绝（`recipe_not_admitted`、未发布的 binding、输入制品未就绪等），
也不评估 host 资源余量（其向量来自请求的 plan）；这些属于具体请求的形状，不是部署状态。

### 与 `/readyz` 的分工

| 面 | 回答的问题 | 变红意味着 |
| --- | --- | --- |
| `/readyz` | 进程与依赖是否健康 | 这个实例不该接流量，负载均衡可以摘掉它 |
| `GET /v1/nexus/jobs/admission-readiness` | 现在创建会不会被拒 | 服务仍能接流量，但创建会被某类原因拒绝 |

两者**互不替代**：`/readyz` 保持只回答进程健康，本路由不改变任何准入判定
（`decide_admission`、配额与回执校验逻辑都没有改动）。

## 2. 轮询者：`job-admission-watch`

参考部署的 base 拓扑里有 `job-admission-watch`（`docker compose up` 即启动，不需要 profile）。
它是一个 HTTP 客户端，只调用上面的路由：

| 项目 | 事实 |
| --- | --- |
| 入口 | `speechnexus-admission-watch`（`backend/src/speechnexus_backend/admission_watch.py`） |
| 依赖 | 仅标准库。**没有**数据库 DSN、没有角色、没有引擎凭据、没有 `depends_on` |
| 输出 | 每轮一行 JSON `{"event":"job_admission_observation",...}`；状态切换额外一行 `job_admission_transition` |
| 状态文件 | `/run/admission-watch/state.json`（私有 tmpfs，原子替换），供 healthcheck 读取 |
| `restart` | `restart: "no"` —— 重启不能续签回执，重启循环只会把降级藏起来 |
| 健康 | 只有“最近一次观测为 `admitting` 且足够新”才 healthy；观测过期即 unhealthy |

配置（`deploy/compose.env.example`）：

- `SPEECHNEXUS_ADMISSION_WATCH_URL`（默认 `http://api:8000/v1/nexus/jobs/admission-readiness?workspace_id=${SPEECHNEXUS_ADMISSION_WORKSPACE:-workspace-default}`，即轮询的是引擎注册默认写入的那个 workspace）
- `SPEECHNEXUS_ADMISSION_WATCH_INTERVAL_SECONDS`（默认 `30`，即降级最多隐藏 30 秒）
- `SPEECHNEXUS_ADMISSION_WATCH_TIMEOUT_SECONDS`（默认 `5`）

退出码（`--once` 与 healthcheck 复用同一分类）：

| 码 | 含义 |
| --- | --- |
| `0` | `admitting` |
| `1` | `degraded`（读到了报告，报告说会被拒） |
| `2` | `unreachable`（读不到报告：连不上、非 JSON、无法识别的 body） |
| `3` | `unavailable`（API 回答自己没有装配 Job 面） |
| `4` | 配置无效（立即退出并报键名，不去轮询） |

“读不到”与“读到了降级”是两件事，必须分开：把前者当成健康，就是本 issue 要消除的那种假绿。

### 它不会重启任何东西

Docker 从不会因为 healthcheck 失败而重启容器，本服务再把 `restart` 显式设为 `"no"`，
并且没有任何其它服务以它为 `depends_on`。因此：

- 降级期间 `docker ps` 会显示 `job-admission-watch (unhealthy)`，同行的 `api` 保持 `Up (healthy)`；
- `docker inspect` 的 `RestartCount` 保持 `0`（轮询容器与被观测的 `api` 都是）；
- 观测进程死亡时容器停在 `Exited(4)` 之类的终态而不是重启循环，healthcheck 也会因为状态文件过期而变红。

### 轮询不等于修复

续签回执必须来自真实引擎探测，因此**没有任何东西能替操作者定时续签**：

```bash
docker compose --profile engine-readiness run --rm engine-readiness-renew
# 或
docker compose --profile engine-readiness run --rm engine-readiness
```

轮询者解决的是“降级不可见”，不是“降级会自动好”。收到 `engine_receipt_expired` 后按上面的命令续签，
下一次轮询会回到 `admitting`。

## 3. 验证

| 证据 | 位置 |
| --- | --- |
| 分类器、三类原因、`expiring` 不降级 | `tests/unit/backend/test_job_admission_readiness.py` |
| 轮询者分类、退出码、状态文件与过期护栏 | `tests/unit/backend/test_admission_watch.py` |
| 契约声明、cause→对外码映射、`/readyz` 保持 200 | `tests/contract/test_job_admission_readiness.py` |
| 真实 PostgreSQL 上的 `admitting → degraded → admitting`、满载、存储故障与未配置 workspace | `tests/integration/test_job_admission_readiness_pg.py` |
| 参考部署仍然装配报告与轮询者 | `tests/ci/test_reference_deploy_job_admission_watch.py` |
