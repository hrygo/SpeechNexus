# 最小同构集群装配

本目录承载「MegaCube 算力面 + 本机 k8s 应用面」的可执行装配。目标、切片与验收口径见
[最小同构集群实施规划](../plans/2026-11-20-minimal-isomorphic-cluster.md)；本文件只写实际操作。

本文件同时记录应用面迁入 MegaCube k3s 的当前状态：**就绪两个 CronJob 已经在集群里跑**，其余应用面单元
仍在本机 k8s（#1250 的派发器唯一持有者）。整体迁入要连数据面一起搬，步骤、回退与判据在
[#1280](https://github.com/hrygo/SpeechNexus/issues/1280)（父项 #338）里，未获批准前不执行。

## 拓扑与端口

| 单元 | 位置 | 访问地址 |
| --- | --- | --- |
| PostgreSQL | 本机 k8s `speechnexus` | `postgres.speechnexus.svc:5432` |
| 对象存储 | 本机 k8s `speechnexus` | `minio.speechnexus.svc:9000` |
| Speech API | 本机 k8s `speechnexus` | `api.speechnexus.svc:8000` |
| 桥接 | 本机 k8s `speechnexus` | `bridge.speechnexus.svc:18010` / `:18020` |
| 执行网关 | MegaCube | `127.0.0.1:8010`（经隧道 `:18010`） |
| 节点控制器 | MegaCube k3s | `127.0.0.1:8022`（`hostNetwork`，与网关、桥同一环回） |
| 推理引擎 | MegaCube k3s | Pod 的 `:18190`，宿主经 `engine-shim` 暴露 `127.0.0.1:18190` |
| 就绪报告与续租 | MegaCube k3s `speechnexus` | CronJob，`hostNetwork`，见下 |

MegaCube 侧 `9000`、`15432`、`18100` 已被既有服务占用，隧道端口固定为
PG `15440`、S3 `19100`，不得改回常用值。应用面的就绪单元在集群里读的就是这两个端口：
它们指向本机那套数据库与对象存储，**不是**集群内的新实例。

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

## 数据面放在哪里

结论：**PostgreSQL 与对象存储留在这套既有的、正在服务的实例上；只把不持有数据面的应用面单元搬进集群。**
把它们换成集群内 PVC 上的新实例是目标形态，但它必须与算力面同批完成，理由是两个共享点：

1. **协调状态只有一份。** 租约、账本、就绪收据都在同一个 PostgreSQL 里，执行网关与派发器读写的是同一批表。
   另起一个集群内 PG，就是第二个协调域：准入在一边通过、派发在另一边领不到，任何一侧单独搬都只会
   得到「Pod 起来了、业务不通」。
2. **对象存储地址写在算力面网关的私有配置里。** `deploy/cluster/compute/gateway-file-asr.json` 的
   `objects.endpoint` 是 `http://127.0.0.1:19100`，没有环境变量可以覆盖。换对象存储就得改这份文件——
   它属于算力面装配，不属于应用面清单。

| 方案 | 成本 | 风险 | 判据 |
| --- | --- | --- | --- |
| 集群内 PVC（PG + MinIO） | 迁数据（dump/restore）、`provision-roles.py` 重建角色、改 `compute.env` 的 PG DSN、改上面那份网关配置、停本机旧应用面 | 网关重启窗口内执行链路不可用；迁错一步就要从 dump 回滚 | 搬完派发是**本地库**事务，才是 #1250 的 5 s 判据 |
| 隧道到既有实例 | 无迁移，改渲染出的 DSN 与 endpoint | 派发器跨隧道 claim，一轮 ≈115 s 撞 120 s 准入截止，把 **#1250 原样搬进集群** | 只适合不领 Job 的只读/续租单元 |

因此落地顺序是「先只搬不领 Job 的单元」：`engine-readiness` 与 `engine-readiness-renew` 只读账本、
只重签一份收据，跨隧道是它们的正常形态；派发器与 API 要等数据面同批进集群。

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

## 部序

### 一级：就绪报告与续租（已在集群内）

前置：`.runtime/cluster/compose.env` 已由 `init-env.py` 生成（含 `sn_*` 角色口令）；
MegaCube 宿主的反向隧道在监听（`ss -ltn` 可见 `127.0.0.1:15440`、`127.0.0.1:19100`）；
`speechnexus-python:<sha>` 已进入 k3s containerd。

```bash
export KUBECONFIG=<指向 MegaCube k3s 的 kubeconfig>
python3 deploy/cluster/app-plane/render-config.py --topology tunnel
python3 deploy/cluster/app-plane/render-manifests.py --select engine-readiness,engine-readiness-renew
kubectl apply -f .runtime/cluster/app-plane/selected.yaml
```

`--topology tunnel` 只渲染 `readiness-env` 与 `private-config` 两个 Secret：数据库指向
`127.0.0.1:15440`、对象存储 `127.0.0.1:19100`、引擎健康地址 `127.0.0.1:18190/health`。
两个 CronJob 以 `hostNetwork: true` + `dnsPolicy: ClusterFirstWithHostNet` 运行——这三个地址都在宿主
环回上，集群网络里的 Pod 一个也够不着；`ClusterFirstWithHostNet` 让 Service DNS 仍然可用，所以把引擎
地址换成 Service（`engine-service.yaml`，`engine-asr-1:18190`）不需要改别的。

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

角色授权在迁移之后执行，SQL 以[最小 Compose 手册 §6](../../../docs/operations/compose-minimal.md#6-创建非-owner-运行角色与最小授权)
为准：把该节 `uv run --all-packages python - <<'PY'` 的代码块原样取出执行（不要在 k8s 侧另写一份
授权 SQL，否则同一规则就有了两个定义点），`SN_ADMIN_DSN` 指向 `127.0.0.1:<本地转发端口>`；
Job 三角色用 `scripts/dev-env/provision-job-roles.py`，三个 Job DSN 先写回 `compose.env`。

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

桥接 Pod 是两台主机之间唯一通道；Mac 休眠或 Docker Desktop 重启会同时停掉 k8s 与隧道。应用面的
就绪 CronJob 在集群里，但它们的数据库、对象存储与引擎健康地址都经这条隧道，所以桥一停，续租
同样失败。本拓扑用于功能验证，不是高可用形态。