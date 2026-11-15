# Job admission observability

「创建 Job 是否可准入」现在是一个**可轮询的信号**，不再需要发一次创建请求才能看到拒绝。

`POST /v1/nexus/jobs` 在引擎 readiness 回执滑出有效期后返回 `503 engine_not_ready`，而
`/readyz`、容器 healthcheck 和 `docker ps` 全部保持绿色。原因是它们回答的不是同一个问题：
`/readyz` 回答“这个进程还能不能服务流量”，准入回答“这一次创建会不会被拒”。把准入塞进
`/readyz` 会让一次只影响创建的事件把整个 API 从负载均衡摘掉（#828 review B8 的结论，
shared `contracts/operations.openapi.yaml` 也因此不改）。

本文档描述这条信号的两个面：**报告**（API 上的只读路由）、**轮询者**（参考部署里真正在读它的容器），
以及让轮询者「有没有在跑」变成会失败的那一步——**验收**。

两个边界先写在前面，免得后来人误读这套东西的能力：

- **「状态可读」≠「状态会被看到」。** 参考部署没有日志采集，也没有告警消费者。这一版做到的是：
  有人去看的那一刻，状态是机器可读的，并且能分清 `degraded` / `unreachable` / 观测过期三种结论。
  #845 消灭的是「完全没有信号」，**不是**「信号会被主动推送」。
- **它不报「进程坏了」，只报「建 Job 的门是开的还是关的」。** 进程是否还能服务流量仍然只看 `/readyz`。

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

### 机器可读原因（闭合枚举）

`causes[]` 的成员就是机器可读的类，不要解析 `detail` 里的句子。这些类**不是**第二套判定：
它们由 `decide_admission` 自己抛出的部署态谓词（`deployment_refusals`）映射而来，
所以「报告说 `admitting`、创建却返回某个码」这种漂移在结构上不成立。

| `cause` | 含义 | 创建时的对外回答 | 处置 |
| --- | --- | --- | --- |
| `engine_receipt_expired` | 创建会钉住的那个 ready 实例，readiness 回执已滑出有效期 | `503 engine_not_ready`，`retryable: false` | `speechnexus-register-engine --renew-readiness` |
| `engine_state_mismatch` | 实例落库的 intent 无法解码或与准入预期不符，钉住的身份不可信 | `503 engine_state_mismatch`，`retryable: false` | 重新注册或协调该实例 |
| `engine_resource_disabled` | 实例、其 pool（含 `pool_state` 与 owner 租约）或已批准配方不处于 active | `503 engine_resource_disabled`，`retryable: false` | 恢复 pool / 重新批准配方 |
| `capacity_exhausted` | 配额已占满、实例 slot 已满，或**根本没有 ready 实例可服务创建** | `429 job_capacity_exhausted` 带 `Retry-After` | 等被持有的 attempt/slot 释放；「没有实例」这一支只能由运维恢复，客户端无限重试只会看到同一个 429 |
| `workspace_not_found` | 该 workspace 没有 `speech_workspaces` 行 | `422 workspace_not_found` | 创建该 workspace |
| `workspace_disabled` | workspace 行存在但状态不是 `active` | `422 workspace_disabled` | 启用该 workspace |
| `workspace_not_configured` | workspace 存在但没有配额行，准入无处记账 | `422 workspace_not_configured` | 为该 workspace 注册引擎以写入配额行 |
| `storage_unavailable` | 在准入身份上读不到协调库，因此其它事实也读不到；**单独出现** | `503 job_unavailable`，`retryable: false` | 恢复数据库读权限/连接 |

前七类来自创建路径同一份判定；第八类是**这次读本身**失败，不是创建被拒，因此单独出现。
`expiring`（仍在预警窗口内但尚未过期）**不是** cause，它仍然可以准入。

一个实例正常不足以让报告说 `admitting`：创建按阶段钉住实例，因此**任一条可用实例链路是干净的**
才报 `admitting`；全部链路都被拒时，报告给出拒绝项最少的那条链路（同数时按码排序）对应的类。

报告不覆盖**请求相关**的拒绝（`recipe_not_admitted`、未发布的 binding、输入制品未就绪等），
也不评估 host 资源余量（其向量来自请求的 plan）；这些属于具体请求的形状，不是部署状态。

### 与 `/readyz` 的分工

| 面 | 回答的问题 | 变红意味着 |
| --- | --- | --- |
| `/readyz` | 进程与依赖是否健康 | 这个实例不该接流量，负载均衡可以摘掉它 |
| `GET /v1/nexus/jobs/admission-readiness` | 现在创建会不会被拒 | 服务仍能接流量，但创建会被某类原因拒绝 |

两者**互不替代**：`/readyz` 保持只回答进程健康，本路由不改变任何准入判定
（`decide_admission`、配额与回执校验逻辑都没有改动）。

## 2. 轮询者：`job-admission-watch`（`--profile admission-watch`）

`job-admission-watch` 是 **opt-in profile**：base 拓扑（`postgres, minio, minio-init, api, gateway`）
只保留「服务流量所必需」的组件。把一个「缺席也不会让任何东西失败」的观测者放进 base，等于用一个
不成立的断言换一个更小的面——那正是 #845 要消除的形态。所以它进 profile，而「它必须在跑」由下一节的
验收步骤保证：**profile 不是安全来源，验收检查才是。**

它和 `api` 用**同一个仓库镜像**（`deploy/images/python.Dockerfile`，`build: *python-build`）：
脚本以 console script 装在镜像里（`backend/pyproject.toml` 的 `project.scripts`），
**没有 bind-mount，也不依赖从哪个目录执行 compose**（`context: ..` 相对 compose 文件解析）。
构建上下文与 `api` 相同，所以两者不会各自漂移。

它是一个 HTTP 客户端，只调用上面的路由：

| 项目 | 事实 |
| --- | --- |
| 入口 | console script `speechnexus-admission-watch`（实现 `backend/src/speechnexus_backend/admission_watch.py`，装在镜像里，不挂载） |
| 镜像 | 与 `api` 同一个构建（`deploy/images/python.Dockerfile`），不额外引入基础镜像 |
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

- 降级期间 `docker compose ps` 会显示 `job-admission-watch ... (unhealthy)`，同行的 `api` 保持 `Up (healthy)`；
- `docker inspect` 的 `RestartCount` 保持 `0`（轮询容器与被观测的 `api` 都是）；
- 观测进程死亡时，容器停在 `Exited(4)` 之类的终态，**不会进入重启循环**。Docker 不再评估已退出容器的
  healthcheck，所以它既不会变红也不会变成 `unhealthy`——要区分「已退出」和「运行中但降级」必须看
  `docker ps` 的 `State`/`Status` 列（`Exited (4)` 对比 `Up ... (unhealthy)`），不能只看 health 字样。

**`unhealthy` 只说明「不可准入（含读不到）」，不说明容器坏了。** `docker ps` 本身**不区分**这两件事，
它只是指针；查因用 healthcheck 自己的输出（保留在 health log 里）：

```bash
docker inspect --format '{{json .State.Health}}' "$(docker compose -p speechnexus ps -q job-admission-watch)" |
  python3 -c 'import json,sys; h=json.load(sys.stdin); print(h["Log"][-1]["Output"])'
```

输出是 JSON，`state` 给出三种不同结论：`degraded`（创建会被拒，`causes` 说明哪一类）、
`unreachable`（读不到报告）、以及 `reason` 为观测过期的「循环已经不再产出」（读不到 ≠ 降级）。
完整的逐轮观测在容器日志里（`job_admission_observation`，状态切换时额外一行
`job_admission_transition`），但那需要 `docker logs`——**这也是上面那条边界的具体含义：
它可查，不保证被看到。**

## 3. 参考装配与验收

参考启动命令（`deploy/compose.yaml` 的 profile 形式）：

```bash
docker compose -f deploy/compose.yaml --profile admission-watch up -d
```

**验收清单**（缺任一步即视为观测未装配）：

1. `docker compose -f deploy/compose.yaml --profile admission-watch ps` 里 `job-admission-watch`
   必须是 `running` 且 `(healthy)`。它是 `Exited (N)`、`Restarting` 或者根本不出现，说明这个部署
   **没有**在读准入，后面的「报告是 200」不算数（已退出的容器没有 healthcheck 状态，`docker ps`
   的 `Status` 列直接给出 `Exited (N)`）。
2. `curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/v1/nexus/jobs/admission-readiness`
   必须是 `200`；非 `200` 说明报告本身没装配（`503 job_unavailable`），不是准入门关着。
3. `curl -sS http://127.0.0.1:8000/v1/nexus/jobs/admission-readiness` 的 `status`/`causes` 必须自洽：
   `causes` 为空当且仅当 `status` 为 `admitting`。
4. `docker inspect` 的 `RestartCount` 为 `0`，且 `api` 的 healthcheck 仍指向 `/readyz`
   （准入降级不该把 `api` 从负载均衡摘掉）。

第 1 步是这套东西里唯一会把「观测者缺席」变成一次可见失败的地方：没有它，profile、文档、
报告都可以是对的，而部署里什么都没有。

### 轮询不等于修复

续签回执必须来自真实引擎探测，因此**没有任何东西能替操作者定时续签**：

```bash
docker compose --profile engine-readiness run --rm engine-readiness-renew
# 或
docker compose --profile engine-readiness run --rm engine-readiness
```

轮询者解决的是“降级不可见”，不是“降级会自动好”。收到 `engine_receipt_expired` 后按上面的命令续签，
下一次轮询会回到 `admitting`。

## 4. 验证

| 证据 | 位置 |
| --- | --- |
| 分类器、报告与创建路径的等价性（含 draining 反例）、`expiring` 不降级 | `tests/unit/backend/test_job_admission_readiness.py` |
| 轮询者分类、退出码、状态文件与过期护栏 | `tests/unit/backend/test_admission_watch.py` |
| 契约声明、cause→对外码映射、`/readyz` 保持 200 | `tests/contract/test_job_admission_readiness.py` |
| 真实 PostgreSQL 上的 `admitting → degraded → admitting`、满载、存储故障与未配置 workspace | `tests/integration/test_job_admission_readiness_pg.py` |
| 参考部署装配报告与轮询者、profile 与验收步骤不被悄悄删掉 | `tests/ci/test_reference_deploy_job_admission_watch.py` |
