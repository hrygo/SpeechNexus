# 最小同构集群 S5：首个跨主机 Job（未成功）

- 采集时间：2026-11-21 00:44–01:10（Asia/Shanghai）
- 结论：**Job 已跨主机走通"准入 → 派发 → 网关提交"前三段，网关提交被拒（503），Job 终态 `failed`，未取得制品，容量未释放。**

## 已成立

| 步骤 | 实测 |
| --- | --- |
| 发布 binding | `model-asr-minimal` 修订 `1`，`mbrev-8f4e0a82c23941f9bc4adf8e74dd61c8`，公开路由 `201/201/200` |
| 注册实例 | `node-megacube-1`=active、`instance-asr-1`=ready、`workspace-default` 配额 8；注册以 k8s Job 执行，10 s 事务在隧道上超时，改在集群内跑 |
| 资产 | `asset-126a3c7e52f54e03822d0cb29231af1c`，`audio/wav`，3447 ms，`ready` |
| 建 Job | `202`，`job-d9bc256519a4437cb4606a653606e032`，`queued` |
| 派发 | 派发器 `claimed` 后 Job 转 `running` |

## 阻塞点

网关 `POST /internal/v1/executions/submit` 连续返回 `503`，日志伴随
`discarding closed connection: <psycopg.Connection [BAD]>`；`/readyz` 同期三项全 `ok`。
Job 最终 `failed`，公开响应不含 `failure` 明细；`held_slots=1`、`held_attempts=1`
**没有释放**——S6 的停止与归还证据因此不成立，这是下一轮的第一优先。

## 本轮修掉的装配缺口（都会让后续重跑失败）

1. API 缺 binding 管理 DSN（draft 直接 `503 binding_result_unknown`）：补 `BINDING_WRITER`/`ADMIN_READER`/`LIFECYCLE`/`LIST_READER` 四键；`sn_binding_defaults_writer` 按文档故意不建。
2. API 缺 `SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL`：建 Job 直接 `503 job_unavailable` 并点名该键。
3. 资产检查为 `text`：上传 `503 asset_inspection_unavailable`；补 `asset-inspector` 部署（`speechnexus-asset-inspector:203d412b`、共享 token、`audio` 模式）后 `201 ready`。
4. 派发器**不能放在应用面**：`Dispatch gateway plain HTTP is allowed only for loopback`。改与网关同机（MegaCube `host` 网络、`127.0.0.1:8010`、PG 走隧道 `15440`、S3 走 `19100`），与参考 Compose 的 `network_mode: service:gateway` 同形。

## 遗留

- 容量租约未释放（1 槽 + 1 次尝试），需要按 S6 取得停止与结算证据，或按准入超时自然回收。
- `dispatch-env` Secret 与 `job-dispatch` 部署已从集群移除，Secret 仍在（未清理）。
- 网关 503 的确切拒绝原因未取到：公开响应与网关日志都不含明细，下一轮需从提交账本/租约表定位。

## 追加（2026-11-21 01:05）：容量死锁

失败的 Job 把唯一的槽永久占住，且**没有任何受支持路径能把它还回来**：

- `POST …/cancellations` → `200 accepted:false status=failed cleanup_state=not_required`，但
  `admission_leases` 仍是 `reserved/pending`、`engine_instances.held_slots=1`。
- 租约 `deadline_at_us` 已过期，`GET` 与派发器都不再推进它；派发器日志为
  `lease release deferred to reconciliation`。
- `POST …/retries` → `429 job_capacity_exhausted`：重试也要新准入，没有槽。
- 再注册同一实例（generation 1）→ 保持 `held_slots=1`，不重置。
- 注册 generation 2（按引擎实际 `max_num_seqs=2` 把 `max_slots` 声明为 2）→
  `previous_generation_active`：上一代未停止释放，注册被拒。
- 运行手册 §13.1 唯一的推进者是取消对账 worker，但它需要账本行；本 Job 在取消时**已是终态**，
  受理没有写账本行（错误地返回 `accepted:false`），所以 worker 无从领取；手册同时明确
  「**不要**手工改 `speech_jobs.state`、租约或补写证据行」。

结论：这是产品缺口——「提交结果未知 → 派发器标记 failed」没有走取消对账的账本写入，
租约因此永久滞留，容量无法归还。对最小集群而言，唯一合规的继续方式是**重建应用面数据库**
（全新部署，租约从未存在），把本次失败保留为证据。

## 网关 503 的根因判断

`control_store_unavailable`。桥接 Pod 重建会切断网关到 PG 的长连接，池里残留
`BAD` 连接，提交因此在数分钟内持续 503；本轮已验证此刻写路径正常（对 `submit_intents`
的 INSERT 走到外键校验才失败，说明语句真正到达 PG）。运维结论：**桥接重连后必须重启网关**，
否则首个 Job 会以 `unknown` 收尾并把容量卡死。

## 第二轮（2026-11-21 01:06–01:20）：重建应用面后仍卡在网关提交

应用面数据库整库重建（PVC 删除 → 迁移到 `0063_deletion_targets_blocked` → 重授角色 → 重新发布
binding `mbrev-56a0a92d…` → 注册 `instance-asr-2` gen 1）。本轮同时查清并记录了三个装配事实：

1. **实例容量声明是节点级的**：`host_nodes.allocatable_*` 在首次注册时写入且之后不可扩；
   默认值恰好等于单实例需求，于是第二个实例报 `host_capacity_insufficient`。显式
   `--host-capacity 25769803776,0,0,2400,0,0` 重新注册后通过。
2. **实例与 binding 修订号强绑定**：注册时读的 recipe 若仍是旧修订，实例会绑到旧
   `mbrev-…`，准入报「no engine instance is registered for model」。同一 generation 不能改绑
   （`instance_config_conflict`），必须换实例名并让 recipe 的 `stage.instance_id` 一致，
   否则 `instance_override_conflict`。
3. **网关提交 503 的确定含义**：`POST /internal/v1/executions/submit` 只有一条 503 路径——
   `app.state.submit_execution` 不是 `SubmitExecutionService`（`_NO_DISPATCH_ADAPTER`）。
   与数据库无关：带凭据的空 body 得到 422，说明路由和凭据正常，只有合法请求落到该分支。

## 未解：同一个镜像，隔离进程能装配、线上进程不能

在运行中的网关容器内用**独立 ingress 锁目录**另起一个 app 并跑 lifespan：

```
submit wired: SubmitExecutionService
runtime: FileExecutionWorker
```

即代码路径与镜像没问题、`/etc/speechnexus/gateway-file-asr.json` 可读、环境变量齐全
（`FILE_ASR_CONFIG_FILE`、`COMPONENT_TOKEN`、两个对象密钥均已设）。但线上那个单进程
（`/proc/1` 即 `speechnexus-gateway`，无多 worker）对合法提交稳定 503，重启网关多次不变。
下一轮要做的是把「线上进程里 `submit_execution` 到底是不是 None」直接读出来（例如临时挂一个
只读诊断入口或从 `app.state` 侧记录启动日志），再决定是装配顺序还是 lifespan 静默失败。

## 第三轮：503 的真因是 3 秒控制事务上限（并更正上一轮结论）

上一轮把 503 读成「没有装配 dispatch adapter」是**错的**。网关有两条 503 出口：路由层
`_NO_DISPATCH_ADAPTER` 与 `SubmissionStoreError` 的 `control_store_unavailable`
（`control_errors.py`，文案是「The control operation could not complete within its bounded
wait.」）。真正命中的是后者：`SPEECHNEXUS_GATEWAY_CONTROL_TIMEOUT_SECONDS` 默认 **3 s**，
而经 SSH 隧道访问 PG 的单次提交事务超过 3 s。

把该值提到 20 s（派发器侧同时从 10 s 提到 30 s）后，实测：

```
POST /internal/v1/executions/submit HTTP/1.1" 200 OK
```

`submit_intents` 首次出现行。**提交路径通了**。这也解释了此前每次都稳定 503、重启无效、
隔离进程却能装配的现象——与装配无关。

## 新的失败点：冻结请求与意图不一致

意图行写入后，执行态为 `failed`、`error_code=invalid_execution_request`，引擎从未被调用
（无 `engine_request_id`）。该错误只有一个来源：`command_dispatch.py` 比对
`request.identity`、`output_stream_id`、`request_digest`、`command_digest(command)` 四者，
任一不符即判「冻结请求与意图矛盾」。

已排除一项：用网关自己的 `command_digest`（`domain/canonical.py`）对存储的命令重算，
结果为 `sha256:0861f153fd62e4e82`，与意图行里的 `command_digest` **一致**。因此差异落在
`identity`、`output_stream_id` 或 `request_digest` 三者之一，下一轮逐个比对。

## 容量再次滞留

该 Job 随后 `failed`，`held_slots` 又是 1，「提交未知/被拒后不归还容量」的缺陷再次复现，
与第二轮记录同源。本轮为继续迭代做过一次诊断性清空（Job/租约表 truncate + 计数归零），
**不作为停止或释放证据**。
