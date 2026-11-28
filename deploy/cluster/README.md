# 最小同构集群装配

本目录承载「MegaCube 算力面 + 本机 k8s 应用面」的可执行装配。目标、切片与验收口径见
[最小同构集群实施规划](../plans/2026-11-20-minimal-isomorphic-cluster.md)；本文件只写实际操作。

本文件同时记录应用面迁入 MegaCube k3s 的当前状态：**协调库与就绪两个 CronJob 已经在集群里**，其余应用面
单元仍在本机 k8s（#1250 的派发器唯一持有者）。整体迁入的步骤、回退与判据在
[#1280](https://github.com/hrygo/SpeechNexus/issues/1280)（父项 #338）里；本次已执行到「只切读侧」，
写侧（本机 api/dispatch 等）停在切写之前，见下文「数据面进集群」。

## 拓扑与端口

| 单元 | 位置 | 访问地址 |
| --- | --- | --- |
| 协调库 PostgreSQL | MegaCube k3s `speechnexus` | `postgres.speechnexus.svc:5432`（自带 PVC，见下） |
| 对象存储 | 本机 k8s `speechnexus` | `minio.speechnexus.svc:9000`；集群侧经隧道 `127.0.0.1:19100` |
| Speech API | 本机 k8s `speechnexus` | `api.speechnexus.svc:8000` |
| 桥接 | 本机 k8s `speechnexus` | `bridge.speechnexus.svc:18010` / `:18020` |
| 执行网关 | MegaCube | `127.0.0.1:8010`（经隧道 `:18010`） |
| 节点控制器 | MegaCube k3s | `127.0.0.1:8022`（`hostNetwork`，与网关、桥同一环回） |
| 推理引擎 | MegaCube k3s | Pod 的 `:18190`，宿主经 `engine-shim` 暴露 `127.0.0.1:18190` |
| 就绪报告与续租 | MegaCube k3s `speechnexus` | CronJob，`hostNetwork`，读集群内协调库 |

MegaCube 侧 `9000`、`15432`、`18100` 已被既有服务占用，隧道端口固定为 S3 `19100`（PG 的 `15440`
只剩本机应用面与回退路径使用，集群侧的读侧单元不再经过它），不得改回常用值。

集群侧仍在隧道上的只有一个服务：**对象存储**。它的端点在算力面网关的私有配置里
（`deploy/cluster/compute/gateway-file-asr.json` 的 `objects.endpoint`），换端点必须与算力面同批改，
不是应用面清单能单独决定的。

## 身份

| 项 | 取值 |
| --- | --- |
| `node_id`（应用面） | `node-app-1` |
| `node_id`（算力面） | `node-megacube-1` |
| `gateway_id` | `gateway-megacube-1` |
| `instance_id` | `instance-asr-1`（代次由控制器账本决定，见下） |
| `pool_id` | `asr-batch` |
| 工作区 | `workspace-default` |

代次（`generation`）不写进任何清单：控制器每次 prepare 一个代次，账本里的
`runtime_instance_heads.current_generation` 才是权威。续租作业因此不带 `--generation`，
由 `speechnexus-register-engine` 自己去读；只有**第一次**登记（还没有 head）要显式给一个，
否则命令以 `generation_required` 拒绝并说明这一点。把代次写死在清单里，会在控制器换代之后
继续给旧代次续命，或者每次续租都被 `stale_instance_generation` 拒绝——两种都不会有人发现，
直到回执耗尽（[#1253](https://github.com/hrygo/SpeechNexus/issues/1253)）。

账本里的那个数**不等于**节点实际准备好的代次：控制器在自己的意图库里另记一套，两边没有任何人
把它们对齐。实测过一次两边分别读 1 和 11，而真正在服务的 Pod 是第 11 代——续租照旧成功、报告照旧
说 `valid` 且还剩 24 小时。因此两个就绪作业都带 `SPEECHNEXUS_CONTROLLER_API_URL` 与
`SPEECHNEXUS_CONTROLLER_API_TOKEN`（后者取自 `controller-component-token`，不复制第二份令牌），
先在签名或判定之前问节点「你准备好的是哪一代」：

- 两边不一致：续租以 `instance_generation_mismatch` 失败并同时报出两个数字与两条出路（把节点那代
  登记进账本，或按账本那代重新 prepare）；报告把该行降级为 `invalid`、`reason` 为
  `instance_generation_mismatch`，不再等 24 小时窗口自然到期。
- 节点问不到：续租以 `controller_unreachable`（或 `controller_refused`）失败，报告把该行降级为
  `invalid`。未知不算健康，跳过检查就会签名出与 #1282 一模一样的「绿而错」。
- 两个变量**都不设**时检查不执行，这是本地 compose 形态；集群里两个都设，缺一个直接报
  `controller_not_configured`，不留半套配置。

## 数据面放在哪里

结论：**协调库（PostgreSQL）在 MegaCube k3s 内自己的 PVC 上，与它仲裁的执行同一网络域；对象存储留在
本机这套正在服务的实例上，集群侧经隧道读。** 两个共享点决定了「哪些能单独搬」：

1. **协调状态只有一份。** 租约、账本、就绪收据都在同一个 PostgreSQL 里，执行网关与派发器读写的是同一批表。
   另起一个集群内 PG，就是第二个协调域：准入在一边通过、派发在另一边领不到，任何一侧单独搬都只会
   得到「Pod 起来了、业务不通」。所以搬库必须是一次性冻结迁移，不是新起一个空库让组件各自改 DSN。
2. **对象存储地址写在算力面网关的私有配置里。** `deploy/cluster/compute/gateway-file-asr.json` 的
   `objects.endpoint` 是 `http://127.0.0.1:19100`，没有环境变量可以覆盖。换对象存储就得改这份文件——
   它属于算力面装配，不属于应用面清单，因此与协调库分两次做。

| 方案 | 成本 | 风险 | 判据 |
| --- | --- | --- | --- |
| 集群内 PVC（PG + MinIO） | 迁数据（dump/restore）、`provision-roles.py` 重建角色、改 `compute.env` 的 PG DSN、改上面那份网关配置、停本机旧应用面 | 网关重启窗口内执行链路不可用；迁错一步就要从 dump 回滚 | 搬完派发是**本地库**事务，才是 #1250 的 5 s 判据 |
| 隧道到既有实例 | 无迁移，改渲染出的 DSN 与 endpoint | 派发器跨隧道 claim，一轮 ≈115 s 撞 120 s 准入截止，把 **#1250 原样搬进集群** | 只适合不领 Job 的只读/续租单元 |

因此落地顺序是「先搬不领 Job 的读侧，再搬写侧」：`engine-readiness` 与 `engine-readiness-renew` 只读账本、
只重签一份收据，先把它们移到集群内协调库并验绿；派发器与 API 要等写侧同批切（#1280 第 5 步）。

## 数据面进集群（#1280 第 2–4 步，已执行）

四个状态由 `render-config.py --topology` 表达，一次只渲染该状态存在的对象：

| `--topology` | 协调库 DSN | 对象存储 / 引擎健康地址 | 渲染的对象 |
| --- | --- | --- | --- |
| `tunnel` | `127.0.0.1:15440` | `127.0.0.1:19100` / `127.0.0.1:18190` | 只有 `readiness-env` + `private-config` |
| `data-plane` | `postgres:5432`（集群内） | 同上（仍走隧道） | 再加 `postgres-credentials`、`migrate-credentials` |
| `in-cluster` | `postgres:5432` | `minio:9000` / `engine-asr-1:18190` | 全部应用面对象 |

`data-plane` 是切写之前的中间态，**故意不是** `in-cluster` 的子集：渲染写侧 Secret 会在旧库还在服务时
把第二个域装配出来。判据在 `tests/ci/test_render_config_topologies.py`：把两个开关合成一个，`data-plane`
的两条断言立刻红。

```bash
export KUBECONFIG=<指向 MegaCube k3s 的 kubeconfig>
# 目标集群的 kubeconfig 只在宿主 root 可读时，先渲染到目录，再把文件拷到宿主 apply。
python3 deploy/cluster/app-plane/render-config.py --topology data-plane --output-dir .runtime/cluster/data-plane
python3 deploy/cluster/app-plane/render-manifests.py --select postgres,migrate

# 第 2 步：新的协调库（StatefulSet/Service/新 PVC，不复用任何既有卷）
kubectl apply -f .runtime/cluster/data-plane/postgres-credentials.yaml
kubectl apply -f .runtime/cluster/app-plane/postgres.yaml
kubectl apply -f .runtime/cluster/data-plane/migrate-credentials.yaml
kubectl apply -f .runtime/cluster/app-plane/migrate-job.yaml
# 判据：迁移版本 = head（当前 0071_generation_adoptions），空库 speech 基表数 = 88
```

第 3 步是**唯一停机窗口**：停本机 `api`、`job-dispatch`、`admission-watch`、`artifact-retention`
（四个都持有协调库连接），`pg_dump` 后 restore 到集群内库，逐表计数与逐列投影合计相等才继续。
判据的取法不靠固定表清单：表清单与数值列都从 `information_schema` 派生，所以新增一张表或一个预留列
不会漏检。角色授权在恢复之后按 `provision-roles.py` + `scripts/dev-env/provision-job-roles.py`
一次装好（授权取自仓库的授权函数，不从 dump 回放 ACL），再用 `verify-roles.py` 逐身份核验。

第 4 步只切读：重新渲染 `data-plane` 的 `readiness-env`（DSN 变成 `postgres:5432`），apply 后
用 `kubectl create job --from=cronjob/engine-readiness-renew` 与 `... engine-readiness` 各跑一次，
判据是 `readiness.state=valid`、`instance_generation` 等于账本当前代次、`renewed=true`。

**写侧没有切。** 第 5 步（api/dispatch 等改 DSN、跑端到端 Job）与第 6 步（撤隧道、清理本机库）
在 #1280 里按评审推进；在此之前本机库是权威、只读保留，写侧单元保持停止状态，恢复命令见
[回退](#回退)。

## 镜像来源只有一个定义点

应用面的清单**不写任何镜像 tag**，每个 `image:` 都是 `${NAME}` 占位，值来自
`deploy/cluster/versions.env`——算力面 `compose.yaml` 的 `${PYTHON_IMAGE}` 也从同一个文件取值。
一个文件决定整套装配的版本，升级不再需要记住十几处位置（[#1238](https://github.com/hrygo/SpeechNexus/issues/1238)）。

```bash
python3 deploy/cluster/app-plane/render-manifests.py --check   # 只校验，不写文件
python3 deploy/cluster/app-plane/render-manifests.py           # 渲染到 .runtime/cluster/app-plane/
```

`--check` 的判据是「`image:` 必须是占位符」：把任意一处改回 `speechnexus-python:3a6a2683`，它退出码 1 并点名
文件与行号；恢复后退出码 0。行级替换只认清单里出现的占位符，所以 PostgreSQL 探针里的
`"$POSTGRES_USER"` 与 `web.yaml` 里的 `^WEB_(...)$` 不会被当成变量吃掉——整份 `envsubst` 会。

`--select engine-readiness,engine-readiness-renew` 只输出指定对象，用于按级上线；完整部序见下。

## 两面同 tag 是装配判据

应用面清单的 `image:` 全是占位符，值来自 `deploy/cluster/versions.env`；算力面
`.runtime/cluster-compute.env` 的 `PYTHON_IMAGE` 与 `CONTROLLER_IMAGE` 由
`deploy/cluster/init-env.py` 从同一个 `versions.env` 生成。所以「两面同 tag」不是约定而是产物：
升级只改 `versions.env`，`cluster-compute.env` 由生成器写回——**不要手改它里面的 tag**。

两个反例（2026-11-28 实测）：应用面 `465c9f94`、算力面 `913d0e0a` 时，派发把网关回执判成
`unknown/outcome_unknown`，Job 停在 `running` 且容量不回；应用面 `465c9f94` 早于 #1287，
镜像里没有 `speechnexus-lease-reconciler`，`lease-reconciler` CronJob 每次
`StartError: executable file not found in $PATH`，过期租约的清收路径等于不存在。

判据（三条都要过）：

1. 两面镜像里 `speechnexus-lease-reconciler --help` 都能跑——回执契约与清收入口在同一提交里定义，
   所以它同时证明两面同源；
2. `grep -c '^PYTHON_IMAGE='` 在 `versions.env` 与 `.runtime/cluster-compute.env` 里都是 1，取值相同；
3. 改完重建 `config-init` + `gateway`（算力面）与相关 Deployment（应用面），两面的容器
   `docker ps` / `kubectl get pod` 里的镜像 tag 一致；
4. **运行态 tag 与 `versions.env` 一致，而不只是两面彼此一致**：`grep '^PYTHON_IMAGE=' deploy/cluster/versions.env`
   的 tag、`kubectl -n speechnexus get pods -o jsonpath` 里每个 python Pod 的镜像 tag、算力面
   `docker ps` 里 `gateway`/`engine-shim`/`binding-disable-worker` 的 tag 三者相同。
   `versions.env` 是唯一取值点：改 tag 只能「改 `versions.env` → `render-manifests.py` → apply」，
   `kubectl set image` 只是把运行态临时对齐，下次 apply 渲染结果时会被清单里的旧 tag 打回去。

2026-11-28 实例：`versions.env` 写 `fed8a5f3-r1`，而 `job-dispatch`/`workbench-api`/`admission-watch`/
`artifact-retention` 与三个 CronJob 还停在 `089fb9f9-r1`——两面同 tag 判据 2 满足，判据 4 不满足。
统一到 `3be6e650-r1` 后 `kubectl diff -f` 对 `api.yaml`、`workers.yaml`、`workbench.yaml` 均为空。

## 部序

### 一级：就绪报告与续租（已在集群内）

前置：`.runtime/cluster/compose.env` 已由 `init-env.py` 生成（含 `sn_*` 角色口令）；
MegaCube 宿主的反向隧道在监听（`ss -ltn` 可见 `127.0.0.1:19100`；`15440` 只在回退到隧道拓扑时要）；
`speechnexus-python:<sha>` 已进入 k3s containerd。

```bash
export KUBECONFIG=<指向 MegaCube k3s 的 kubeconfig>
python3 deploy/cluster/app-plane/render-config.py --topology data-plane
python3 deploy/cluster/app-plane/render-manifests.py --select engine-readiness,engine-readiness-renew
kubectl apply -f .runtime/cluster/data-plane/readiness-env.yaml
kubectl apply -f .runtime/cluster/app-plane/selected.yaml
```

`--topology data-plane` 渲染的 `readiness-env` 里，数据库是集群内 Service `postgres:5432`，对象存储仍是
`127.0.0.1:19100`、引擎健康地址仍是 `127.0.0.1:18190/health`。两个 CronJob 以 `hostNetwork: true` +
`dnsPolicy: ClusterFirstWithHostNet` 运行——后两个地址在宿主环回上，集群网络里的 Pod 够不着，而
`ClusterFirstWithHostNet` 让 Service DNS 仍然可用，所以数据库这一项可以只改 DSN。

只有对象存储还在隧道上：它的端点由算力面网关的端点文件决定，与协调库不在同一次切换里（见上）。

### 二级：应用面其余单元（未执行）

完整应用面进集群要求数据面同批进集群，清单已经就绪但**不要对空的协调库 apply**：那会造出第二个
域。获批准后按下面的顺序，每级都过判据再进下一级（步骤与回退见 [#1280](https://github.com/hrygo/SpeechNexus/issues/1280)）：

```bash
python3 deploy/cluster/app-plane/render-config.py --topology in-cluster
kubectl apply -f .runtime/cluster/app-plane/postgres.yaml
kubectl apply -f .runtime/cluster/app-plane/minio.yaml
kubectl apply -f .runtime/cluster/app-plane/minio-init.yaml
kubectl apply -f .runtime/cluster/app-plane/migrate-job.yaml     # 一级：迁移成功
kubectl apply -f .runtime/cluster/app-plane/api.yaml             # 二级：api 1/1 且 /readyz 200
kubectl apply -f .runtime/cluster/app-plane/engine-service.yaml
kubectl apply -f .runtime/cluster/app-plane/workers.yaml         # 三级：派发、后台单元、两个 CronJob
```

角色授权在迁移之后执行，角色与最小授权取自 `deploy/cluster/provision-roles.py`（授权函数的唯一调用点；
不要在 k8s 侧另写一份授权 SQL，否则同一规则就有了两个定义点），`SN_ADMIN_DSN` 指向
`127.0.0.1:<本地转发端口>`；Job 三角色用 `scripts/dev-env/provision-job-roles.py`，三个 Job DSN 先写回
`compose.env`。两个脚本都拒绝在角色已存在时重跑，因此它们只用于初次装配。

最后用 `verify-roles.py` 逐身份核验：每个角色用自己的 DSN 登录，并查询一条它确实被授权的
关系。工作台两个角色此时还没有授权目标（授权目标在工作台命名空间），探针只报 `login only`。

## 验收

每一级都有自己的判据；**`kubectl get pods` 全绿不算**应用面可用。

```bash
# 一级：迁移 Job
kubectl -n speechnexus get job migrate -o jsonpath='{.status.succeeded}{"\n"}'   # 1
kubectl -n speechnexus logs job/migrate | tail -3

# 二级：API 自己说它依赖的东西都通
kubectl -n speechnexus get pod -l app=api -o jsonpath='{.items[0].status.containerStatuses[0].ready}{"\n"}'  # true
kubectl -n speechnexus exec deploy/api -- python -c \
  'import urllib.request as u; print(u.urlopen("http://127.0.0.1:8000/readyz").status)'             # 200

# 三级：派发器在领 Job（不是「Deployment 存在」）
kubectl -n speechnexus logs deploy/job-dispatch --tail=20 | grep -E 'claimed|dispatch'
#   一条 queued → running 的 5 s 判据：见 #1250 的验收命令

# 一级（就绪）：续租真的重签了收据
kubectl -n speechnexus create job --from=cronjob/engine-readiness-renew readiness-renew-proof
kubectl -n speechnexus logs job/readiness-renew-proof | head -30
#   → health_probe.status=200、readiness.renewed=true、readiness.state=valid、
#     instance_generation 等于账本当前代次（runtime_instance_heads.current_generation）

# 一级（就绪）：小时报的状态与窗口
kubectl -n speechnexus create job --from=cronjob/engine-readiness readiness-report-proof
kubectl -n speechnexus logs job/readiness-report-proof
#   → {"readiness_state":"valid","remaining_seconds":…,"observed_seconds_ago":…}
#   → {"summary":{"valid":1,"ready_instances":1,…}}
```

`instance_generation` 只来自账本：清单里不写 `--generation`，续租自己读
`runtime_instance_heads.current_generation`。写死会在控制器换代之后继续给旧代次续命，或者每次取租都被
`stale_instance_generation` 拒绝，两种都不会有人发现，直到回执耗尽（#1253）。

切读之后（`--topology data-plane`、`data-plane` 的 `readiness-env`），这条命令读写的已经是集群内协调库。
所以判据不只 `state=valid`：还要看**收据落在哪一边**——两库 `runtime_instance_intents` 里同一条意图的
`readiness_document`（`verified_at_us`）只能有一边前进。两边都前进就是两个协调域，按事故处理。

## 回退

一级（就绪两个 CronJob）：

```bash
kubectl -n speechnexus delete cronjob engine-readiness engine-readiness-renew
kubectl -n speechnexus delete secret readiness-env private-config      # 可选：只影响这两个单元
```

本机应用面与算力面都不受影响；删掉 CronJob 后回执按声明 TTL（24 h）自然过期，风险回到「无人续签」
这个既有状态。

二级（完整应用面进集群）的回退是逐步反向的，含算力面 `compute.env` 与网关重建的反向命令，见
[#1280](https://github.com/hrygo/SpeechNexus/issues/1280)；本切片不执行。

数据面（协调库进集群，第 4 步之后）的回退是把读侧 DSN 改回隧道，集群内库与数据原地保留：

```bash
python3 deploy/cluster/app-plane/render-config.py --topology tunnel
kubectl apply -f .runtime/cluster/data-plane/readiness-env.yaml   # 或按 --output-dir 渲染后 apply
kubectl -n speechnexus create job readiness-renew-rollback --from=cronjob/engine-readiness-renew
```

判据是续租日志回到 `state=valid` 且收据写在本机库（比对两库 `runtime_instance_intents.readiness_document`
的 `verified_at_us`：只有本机库那一行前进）。第 3 步失败时的回退更简单：集群内库丢弃重建，本机库自始至终
没有被写过，改回 DSN 即恢复。

被停的写侧单元（第 3 步的停机窗口）在同一命名空间里按原副本数恢复：

```bash
kubectl --context docker-desktop -n speechnexus scale \
  deploy/api deploy/job-dispatch deploy/admission-watch deploy/artifact-retention --replicas=1
```

停顿期间**只有一个写方向**：本机库冻结、集群内库只接收就绪收据。发现两侧都有写入时按「双协调域」
事故处理，不做自动合并。

删除整个命名空间是最后手段，会连同控制器状态一起删：

```bash
kubectl delete namespace speechnexus
```

## 就绪回执的续期与报警

就绪回执是可准入的依据，它由一次**真实健康探测**签出。有效期是声明（默认 24 h），但准入还看第二个
事实：**这份回执多久以前签的**。超过 15 min 没被重新探测，状态就是 `unobserved`，准入按
`503 engine_not_ready` 拒绝——回执只说明签字那一刻引擎在，不说明现在在。两个事实因此可以分辨：
窗口耗尽报 `expired`，没人看引擎报 `unobserved`。

| CronJob | 频率 | 作用 |
| --- | --- | --- |
| `engine-readiness-renew` | 每 5 min | 探测引擎健康并重签回执（刷新 `verified_at`） |
| `engine-readiness` | 每小时 | 报告回执状态；带 `--fail-on-expiring --warn-within-seconds 21600` |

续租周期必须短于新鲜度窗口（5 min × 3 = 15 min），否则一次漏跑就把实例打成 `unobserved`。
反过来说，**引擎停下之后 15 min 内平台一定停止承诺容量**，不再有「引擎已经没了、准入还在收活」
的窗口（#1263）。

报警信号有两个层次：`unobserved`/`expired` 自己就让准入 503（产品面可见），小时报的 Job
同时变 `Failed`，日志结尾 `error: readiness_unobserved` 或 `error: readiness_expiring`，条目里带
`observed_seconds_ago` 与 `remediation`。故意让它可以红，两条都实测过（`kubectl set env
cronjob/engine-readiness-renew SPEECHNEXUS_ENGINE_HEALTH_URL=…` 改坏地址再来一次，验完用
`… -` 删回）：

```bash
# 把健康地址指到一个不可达端口 → 续租失败并失败退出
# → error: engine_unreachable: health endpoint is unreachable from this operator host

# 指到一个有进程但不健康的口 → 同样红，报的是另一种原因
# → error: engine_unhealthy: health endpoint returned HTTP 404

# 把警戒窗口放大到超过剩余时间 → 立刻转 expiring 并失败
speechnexus-engine-readiness --warn-within-seconds 100000 --fail-on-expiring
# → error: readiness_expiring: 1 instance(s) inside the 100000 s warning window

# 停掉引擎、又不让续租跑 → 15 min 后准入转 refus「engine_not_ready: readiness_unobserved」
speechnexus-engine-readiness
# → {"readiness_state": "unobserved", "observed_seconds_ago": 1080, ...}
# → error: readiness_unobserved: instance-asr-1 g3 (observed_seconds_ago=1080)
```

续租失败时小时报仍会红，但那只是运维视角；真正挡住用户的准入侧在同一时刻已经拒绝，不需要
等人看日志。

## 引擎地址链

引擎是 Pod，但有两个消费者只认宿主本地端口：算力面的执行网关（host 网络）与应用面的就绪续租。
集群网络里的应用面 Pod 两个都到不了，所以这一跳由宿主补：

```
网关(host) 127.0.0.1:18190
   └─ engine-shim（host 网络，转发到 ENGINE_ADDRESS）
        └─ 引擎 Pod <podIP>:18190
就绪续租 Pod（hostNetwork）──> 宿主 127.0.0.1:18190（同上一条链路）
集群内其他单元 ──> engine-asr-1.speechnexus.svc:18190 ──> 引擎 Pod
```

`engine-service.yaml` 是集群内的稳定地址：它按 `speechnexus.io/instance-id` 选当前服务该实例的 Pod，
Pod 自己的就绪探针（`GET /health`，2 s）把正在预热或正在退场的代次挡在端点列表之外。**Pod IP 不进
任何清单**：每换代都变，写进去就是把易变值当常量（#1253）。

`ENGINE_ADDRESS` 是 shim 这条链上**唯一**记录引擎地址的地方，写在
`.runtime/cluster-compute.env`（`<podIP>:18190`）。Pod 重建换 IP 后按顺序做两步即可：

```bash
kubectl -n speechnexus get pod -l speechnexus.io/instance-id=instance-asr-1 -o jsonpath='{.items[0].status.podIP}'
# 写回 .runtime/cluster-compute.env 的 ENGINE_ADDRESS=<podIP>:18190，然后
docker compose -p speechnexus-cluster-compute --env-file .runtime/cluster-compute.env \
  -f deploy/cluster/compute/compose.yaml up -d --force-recreate --no-deps engine-shim
```

只改这一处：网关端点文件读的是 18190 这个宿主端口，不跟随 Pod IP。反过来，把 Pod IP 直接写进
`gateway-file-asr-endpoint.json` 是错的——那样每次重建都要改两处。

端点里的 `target.instance_generation` **不由人手填**：它是账本事实
（`runtime_instance_heads.current_generation`），控制器换代就变，写死会在下一次换代后全程
`stale_instance_generation`，而只读侧仍报 `valid`（#1291）。换代或重建后按顺序做：

```bash
# ① 从账本写回端点文件（只改这一个字段，地址/端口/绑定版本原样保留）
SPEECHNEXUS_DATABASE_URL=... SPEECHNEXUS_DATABASE_SCHEMA=speech \
  python3 deploy/cluster/render-endpoint.py \
  --file .runtime/cluster/compute/gateway-file-asr-endpoint.json
# ② 核对：文件与账本一致时退出 0，不一致时退出 1 并点名两个数字
python3 deploy/cluster/render-endpoint.py \
  --file .runtime/cluster/compute/gateway-file-asr-endpoint.json --check
```

`--check` 是漂移检测：账本有行才算数，账本没有该实例的 head 行时脚本拒绝写任何代次（缺行不是
"第 1 代"）。网关重启前后各跑一次即可；`readiness` 判据里的 `instance_generation` 与它读的是同一
个账本字段。

## 作业派发

派发器在应用面：`deploy/cluster/app-plane/workers.yaml` 的 `job-dispatch` Deployment，
配置来自 `render-config.py` 写出的 `dispatch-env` Secret。**只跑一个副本**——它持的是桶租约，
第二个持有者会把同一批到期 Job 拆成两半。

算力面 `compose.yaml` **不再有** `dispatch` 服务，也不提供这个回退：在那边认领要跨桥，慢的
不只是它自己。

| 派发器位置 | 一轮 claim 耗时 | 结果 |
| --- | --- | --- |
| 算力面（跨桥） | ≈115 s | 撞上 120 s 准入截止，Job 派发前就过期；长事务还把**其他** claim 串行化 |
| 应用面（本地库） | 0.15 s | `queued → succeeded` 约 10 s |

所以「派发器停了」的正确处置是让这个 Deployment 恢复（`kubectl -n speechnexus rollout restart
deploy/job-dispatch`，必要时 `scale --replicas=1`），不是把派发器搬回算力面。要临时在别处跑一份，
必须在数据库旁边、并且先 `scale --replicas=0` 停掉集群里这个——两个持有者会把同一批到期 Job
拆成两半，`job-dispatch` 用的是桶租约而不是进程内锁。

派发器与数据库的相对位置决定 claim 耗时，这也是「应用面整体进集群必须连数据面一起搬」的直接原因。

## 算力面控制器

`deploy/cluster/compute/controller-deployment.yaml` 把控制器作为常驻 k3s 工作负载运行，
取代 `compose.yaml` 里的 `controller` 服务。两者是同一组变量，区别只有三处，都是刻意的：

- `SPEECHNEXUS_CONTROLLER_RUNTIME=k8s`：驱动改为经 API server 建引擎 Pod
- `hostNetwork: true` + `SPEECHNEXUS_CONTROLLER_HOST=127.0.0.1`：网关与桥都是以宿主
  `127.0.0.1:8022` 找控制器的，Pod 共享宿主网络命名空间才能保住这两条路径，同时仍只绑环回。
  代价是探针必须打到 `127.0.0.1`（打 Pod IP 永远连不上，会重启健康的控制器）
- 不挂容器守护进程 socket：选了 k8s 驱动之后，那条路径没有任何调用方

**切换顺序**：先停 compose 的 `controller` 服务，再 apply 本清单——两者都绑 8022，先起后停会
`address already in use` 并进入 `CrashLoopBackOff`。回退反过来：`kubectl -n speechnexus scale
deploy/node-controller --replicas=0`，再 `docker compose ... start controller`。

**接管既有实例的边界**：控制器只对**自己创建过**的 Pod 能收干净。接管一个由别的控制器创建、
没有 prepare 记录的 Pod 时，`stop` 删得掉 Pod 但拿不到退出码，返回 `stop_evidence_unverified` 并进入
quarantine，此后该实例的新代 `prepare` 一律被 `gateway_drain_incomplete` 拒绝（#1261）。所以在
compose → 集群切换时，已经在跑的实例要么先由旧控制器停干净，要么换 instance id，不要让新控制器接管。

清单里只有 `${CONTROLLER_IMAGE}`，与 `compose.yaml` 同一约定，插值来自 `versions.env`。
`component token`、`approved-templates.json` 都由下面两步从既有来源生成，不写进清单：

```bash
# 镜像必须先进入 k3s 的 containerd。二选一，都要求节点权限或一个内网 registry。
docker save speechnexus-node-controller:<sha> | sudo k3s ctr images import -
#   或者：docker push <registry>/speechnexus-node-controller:<sha>，再把清单里的
#   image 换成该地址

# 顺序有约束：模板文件先于镜像升级会被旧控制器**整份拒绝**（文件模型是
# extra="forbid"，它不认识新字段），控制器随后以 CrashLoop 收场。先换镜像、等 rollout
# 成功，再 apply 模板；反过来只需重启一次 Pod，不会进循环。
# 两个输入取自既有来源，不在清单里复制
kubectl -n speechnexus create configmap controller-approved-templates \
  --from-file=approved-templates.json=deploy/cluster/compute/approved-templates.json \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n speechnexus create secret generic controller-component-token \
  --from-literal=token="$(grep '^COMPONENT_TOKEN=' .runtime/cluster/compute.env | cut -d= -f2-)" \
  --dry-run=client -o yaml | kubectl apply -f -

CONTROLLER_IMAGE=$(grep '^CONTROLLER_IMAGE=' deploy/cluster/versions.env | cut -d= -f2) \
CONTROLLER_ENGINE_GPU_MEMORY_MIB=$(grep '^SPEECHNEXUS_CONTROLLER_ENGINE_GPU_MEMORY_MIB=' \
  .runtime/cluster/compute.env | cut -d= -f2) \
CONTROLLER_ENGINE_GPU_TOTAL_MIB=$(grep '^SPEECHNEXUS_CONTROLLER_ENGINE_GPU_TOTAL_MIB=' \
  .runtime/cluster/compute.env | cut -d= -f2) \
CONTROLLER_ENGINE_GPU_MEMORY_UTILIZATION=$(grep '^SPEECHNEXUS_CONTROLLER_ENGINE_GPU_MEMORY_UTILIZATION=' \
  .runtime/cluster/compute.env | cut -d= -f2) \
  envsubst < deploy/cluster/compute/controller-deployment.yaml | kubectl apply -f -
```

`ENGINE_GPU_MEMORY_UTILIZATION` 是引擎在一张卡上的份额，必须显式给：留空时驱动不追加任何参数，
vLLM 按整卡 92% 预留，卡上有别的服务就直接起不来。取值是模板声明的峰值除以卡的总显存
（本机 1.7B 模板 12 GiB / 121.66 GiB ≈ 0.099），不是留给引擎「自己看着办」。

也可以不给份额，改成声明两个数让控制器派生：`GPU_MEMORY_MIB`（额度，同时是 libvgpu 的硬上限）
与 `GPU_TOTAL_MIB`（卡的总量），派生结果向下取整，因此引擎永远不会超过额度。三者都留空时，
带 GPU 的模板会在 prepare 被点名拒绝（`gpu_budget_undeclared`），不会以「不限」的名义启动一台
按整卡 92% 抢显存的引擎。

投产前先停掉 compose 的控制器，否则同一个 `node_id` 上有两个写入者抢同一份意图：

```bash
docker compose -p speechnexus-cluster-compute --env-file .runtime/cluster-compute.env \
  -f deploy/cluster/compute/compose.yaml stop controller
```

回退就是反过来：起 compose 控制器、删掉这个 Deployment。意图存储是 SQLite 单文件，
两侧不共享，切换时在途意图的归属只认最后写入的那一侧。

## 已知单点

桥接 Pod 是两台主机之间唯一通道；Mac 休眠或 Docker Desktop 重启会同时停掉 k8s 与隧道。协调库已经
不在两台主机之间，但就绪 CronJob 的**对象存储**端点仍经这条隧道，所以桥一停，续租仍会失败——
只是失败原因从「数据库不可达」变成「对象存储不可达」。应用面整体进集群（含对象存储换端点）之后
这条依赖才消失。本拓扑用于功能验证，不是高可用形态。

协调库本身是单副本 StatefulSet + `local-path` PVC：节点磁盘故障即丢库，备份策略不在本装配内。