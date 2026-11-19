# 准入就绪巡检（admission-watch）

`admission-watch` 是参考部署里的可选后台单元：它定期读取公开 Job 的准入就绪报告，只在结论变化时写一行结构化日志，并在拒绝持续到阈值时写一行告警级日志。

## 为什么需要它

`GET /v1/nexus/jobs/admission-readiness` 回答的是一个瞬间：如果此刻发一个 create，会不会被放行。它只有在**有人调用时**才回答。没有调用者时，「所有 create 都被拒」与「一切正常」在外部完全一样：`/readyz` 返回 `200`、容器健康检查绿、`docker ps` 正常。可复现的形态是收据过期、被 pin 的实例离开 `active`、配额或槽位占满——创建全部 `503`，而进程看起来健康。

本进程就是那个调用者。它不新增判定：报告本身由 `decide_create_admission` 产生，与创建路径共用同一个函数；巡检只消费报告。

## 它读什么、不做什么

- 调用 `PublicJobs.admission_readiness(workspace_id)`——HTTP 路由处理器调用的**同一个服务对象**，因此不会出现扫描器与端点各有一套规则。
- 每次轮询一个只读事务（`REPEATABLE READ, READ ONLY`），只 `SELECT`：不预留配额、不写任何表、不访问引擎或网关、不打开 HTTP 客户端去打自己的端点。
- 不改 `/readyz` 语义，也不让任何容器因业务原因重启：它只是另一个进程的一段日志，重启也修不好过期收据。
- 不做指标导出，不自动修复（不续签就绪收据、不释放容量、不改绑定）。

## 开了会看到什么

入口在启动时（`SPEECHNEXUS_ADMISSION_WATCH_ENABLED=true` 之后）用仓库共享的
[`json_logging.use_json_logging()`](../../backend/src/speechnexus_backend/json_logging.py)
把 root logger 配成 stderr 上的一行一个 JSON 对象，级别 `INFO`，`extra` 的键成为顶层字段。
也就是说**不装任何格式化器、不做额外配置，容器日志里就是下面这些行**；其它进程若已配置 root logger，
该函数不覆盖。logger 名是 `speechnexus_backend.speech.application.admission_watch`，一共四种事件：

| `event` | 级别 | 写入时机 | 主要字段 |
| --- | --- | --- | --- |
| `admission_watch_started` | INFO | 每个 workspace 在本进程的第一次观测，一行 | `workspace_id`、`status`、`cause`、`interval_seconds`、`alert_after` |
| `admission_watch_changed` | INFO | `status` 或 `cause` 与上一次不同时，恰好一行 | `workspace_id`、`from`{`status`,`cause`}、`to`{`status`,`cause`}、`detail` |
| `admission_watch_alerted` | WARNING | 连续计数第一次达到阈值时，一行（不是每个间隔一行） | `workspace_id`、`cause`、`consecutive_degraded`、`alert_after`、`interval_seconds`、`since_us`、`detail` |
| `admission_watch_cleared` | INFO | 曾经告警、现在不再满足判据时，一行 | `workspace_id`、`status`、`cause`、`alert_after` |

同一状态重复轮询不产生新行。下面是 `tests/integration/test_admission_watch_pg.py`（真 PostgreSQL）用
本进程同一个格式化器打印的实测输出——翻转、重复轮询、翻回三段依次是：

```json
{"event": "admission_watch_changed", "workspace_id": "workspace-test", "from": {"status": "admitting", "cause": null}, "to": {"status": "degraded", "cause": "engine_resource_disabled"}, "detail": "The pinned instance, its pool, its node or the approved recipe is not active, so a create answers engine_resource_disabled until it is re-enabled or re-approved.", "message": "admission readiness changed", "time": "2026-11-19T19:48:44.067096+00:00", "level": "INFO", "logger": "speechnexus_backend.speech.application.admission_watch"}
```

```text
（同一状态再轮询一次：0 行）
```

```json
{"event": "admission_watch_changed", "workspace_id": "workspace-test", "from": {"status": "degraded", "cause": "engine_resource_disabled"}, "to": {"status": "admitting", "cause": null}, "detail": null, "message": "admission readiness changed", "time": "2026-11-19T19:48:44.078939+00:00", "level": "INFO", "logger": "speechnexus_backend.speech.application.admission_watch"}
```

`detail` 来自报告本身，写明该 cause 的人话原因和恢复命令（例如重新签收据要用 `speechnexus-register-engine --renew-readiness`）。

## 判据

告警条件：**连续 N 次轮询**得到 `status=degraded`，且 `cause` 属于部署级。`cause` 按报告的枚举语义分两类，不是按前缀猜的：

| 分类 | cause | 是否叫醒人 | 理由 |
| --- | --- | --- | --- |
| 部署级 | `engine_receipt_expired`、`engine_state_mismatch`、`engine_resource_disabled`、`capacity_exhausted`、`storage_unavailable` | 是 | 部署自己拒绝所有 create：收据过期、运行态需重建、池/节点/配方未激活、没有可接单的实例、协调存储读不出 |
| workspace 级 | `workspace_not_found`、`workspace_disabled`、`workspace_not_configured` | 否 | 在准入被咨询之前就按 workspace 拒绝，属于该 workspace 自身的目录/配额配置；为它叫醒部署值守只会训练人忽略这个信号 |

两条补充：

- `capacity_exhausted` 同时覆盖「workspace 配额满」和「没有一个 ready 实例能接单」。后者正是本巡检要发现的部署形态，所以这一 cause 归部署级；前者在尝试被释放后自愈。如果某些部署的租户负载会长期占满配额，调大阈值或间隔，或把它从告警面排除前先看清是哪一半。
- 分类的补集关系由单元测试钉住：新增一个 cause 而未分类时用例变红，默认方向是「响亮」。

默认值与理由：

- `SPEECHNEXUS_ADMISSION_WATCH_ALERT_AFTER=3`（范围 1–60）
- `SPEECHNEXUS_ADMISSION_WATCH_INTERVAL_SECONDS=60`（范围 1–3600）

create 自己的重试语义是秒级（`Retry-After: 1`），一次采样抖动（例如单个连接超时被记成 `storage_unavailable`）不该叫醒人；3 次 × 60 s 意味着**持续约 2–3 分钟**的拒绝才告警，比 `#845` 里「20 分钟无人知道」的盲区小一个数量级，也给一次登录/一次滚动重启留出了自愈窗口。告警延迟上界约为 `(N-1) × interval + 一次读的耗时`；把 `N` 调到 1 会让第一次采样就告警，只适合已经确信没有瞬时抖动的环境。

状态只存在进程内：重启后从 `admission_watch_started` 重新计数，不保留旧的连续计数或告警锁存。

## 怎么开

compose（推荐）：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  --profile admission-watch up -d
docker compose --env-file deploy/compose.env -f deploy/compose.yaml \
  logs -f admission-watch
```

该服务在 `admission-watch` profile 下，基础 `up` 不会启动它。它读的键：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `SPEECHNEXUS_ADMISSION_WATCH_ENABLED` | `false` | 进程级开关。compose 服务把它传成 `true`（profile 本身就是显式 opt-in）；直接跑入口时默认关闭 |
| `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL` | 空 | 必需。报告读的是准入角色才能看到的预留计数行，Job writer 角色看不到；空值在启动时失败并点名 |
| `SPEECHNEXUS_DATABASE_SCHEMA` | 空 | 必需，与 API 同库同 schema |
| `SPEECHNEXUS_ADMISSION_WATCH_INTERVAL_SECONDS` | `60` | 轮询间隔 |
| `SPEECHNEXUS_ADMISSION_WATCH_ALERT_AFTER` | `3` | 连续多少次部署级拒绝才告警 |
| `SPEECHNEXUS_ADMISSION_WATCH_WORKSPACES` | `workspace-default` | 逗号分隔的 workspace 列表；每个独立记日志、独立计数 |
| `SPEECHNEXUS_ADMISSION_WATCH_TRANSACTION_TIMEOUT_SECONDS` | `3` | 单次只读事务预算 |

非 compose 部署（外部计时器）：

```bash
SPEECHNEXUS_ADMISSION_WATCH_ENABLED=true \
SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL=... SPEECHNEXUS_DATABASE_SCHEMA=speech \
speechnexus-admission-watch            # 常驻循环
speechnexus-admission-watch --once     # 单次；见下方退出码口径
```

`--once` 的退出码与告警判据**同一分类**：`0` = 没有任何 workspace 处于部署级拒绝；`1` = 至少一个
workspace 是部署级拒绝（`engine_*`、`capacity_exhausted`、`storage_unavailable`）；`2` = 巡检没能启动
（配置缺失、DSN 为空、报表关系不可读）。workspace 级拒绝（`workspace_not_found` 等）报告虽是
`degraded`，`--once` 仍返回 `0`——它们是该 workspace 自己的管理员状态，不会叫醒人，变化行照写。
报告层的读失败会被折叠成 `storage_unavailable`，因此是 `1` 而不是 `2`。它与常驻循环的判定和日志格式
相同，只是不再等待下一个间隔（也因此不会累计到告警阈值）。

**它不是健康检查。**`--once` 的退出码只回答「此刻有没有部署级拒绝」，不回答「巡检进程是否活着」；
把非零当容器不健康或让编排重启，会把「业务拒绝」错误地变成「进程故障」，重启也修不好过期收据。

## 关掉时

未启用时 `build_admission_watch` 返回 `None`：进程打印一行 `admission watch disabled` 并以 0 退出，**不建连接池、不产生任何日志、不占用连接**。这与取消对账 worker（`SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_ENABLED`，默认 `false`）的开关形态一致。

## 已知边界

- 告警只是**日志行**。它被采集到哪（journald、日志平台、值班系统）由部署方接线；本仓库没有 Prometheus 导出器，也没有内建通知通道。
- 判据看的是报告的即时结论，不看进程是否活着：巡检进程自己停了就没有日志，这一点需要由部署侧的容器状态/日志新鲜度覆盖，而不是本进程自证。
- 读权限用的是准入身份（与 API 的 `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL` 同值）。本切片没有新增只读角色，因此它的权限面与准入相同。
- 阈值与间隔没有在真实负载下标定；上面的默认值是按「秒级抖动不该叫醒人 + 20 分钟盲区」两个约束倒推的。
- `capacity_exhausted` 同时覆盖「workspace 配额满」：长期配额饱和会让判据持续成立、告警锁存后一直不
  解除。阈值只能把首次告警推迟 `(N-1)` 个间隔，压不住这种持续状态；真正的开关是把该 workspace 从
  `SPEECHNEXUS_ADMISSION_WATCH_WORKSPACES` 移除，或先释放它的配额。
- `--once` 一次只读一轮，不保留任何跨进程状态：cron 每次都会先写 `started` 行，永远不会写 `alerted`。
  需要告警锁存就必须跑常驻循环。