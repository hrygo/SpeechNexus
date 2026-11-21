# 双机开发测试环境：向完整架构对齐

本规划把「MegaCube 算力面 + 本机 k8s 应用面」从「能跑通一次文件 ASR」推进到「支撑后续完整研发」：
单元齐全、装配与参考 Compose 一致、差异只允许以具名白名单存在，并且差异能被机器检查发现。

- 决定依据：[ADR 0014](../decisions/0014-development-environment-isomorphism.md)（开发环境同构与差异边界）
- 架构基准：`docs/architecture/deployment-units.md`（部署单元与边界）
- 参考装配：`deploy/compose.yaml`（profile 化的完整组合）
- 已完成的最小验证：`docs/plans/2026-11-20-minimal-isomorphic-cluster.md`
- 实机证据：`docs/archive/audits/evidence/2026-11-21-minimal-cluster/`

## 1. 差距矩阵

参考 Compose 的每个单元，在双机环境里的现状：

| 单元（参考 Compose） | profile | 双机环境现状 | 判定 |
| --- | --- | --- | --- |
| `postgres` | 默认 | k8s StatefulSet | 一致 |
| `minio`、`minio-init` | 默认 | k8s StatefulSet + Job | 一致 |
| `migrate` | `migration` | Job `migrate` | 一致 |
| `api` | 默认 | Deployment `api` | 一致 |
| `gateway` | 默认 | MegaCube Compose | 一致（隧道替代内网链路） |
| `job-dispatch-worker` | `job-dispatch` | MegaCube Compose，与网关同置 | 一致（参考同样 `network_mode: service:gateway`） |
| `asset-inspector` | `asset-inspection` | Deployment `asset-inspector` | 一致 |
| `engine-register` | `engine-registration` | Job `engine-register` | 一致 |
| `binding-disable-worker` | `binding-disable` | — | **缺** |
| `artifact-retention` | `artifact-retention` | — | **缺** |
| `admission-watch` | `admission-watch` | — | **缺** |
| `engine-readiness`、`engine-readiness-renew` | `engine-readiness` | — | **缺** |
| `workbench-api`、`workbench-migrate` | `workbench-api` | — | **缺** |
| `admin`、`workbench` 前端 | `web` | — | **缺** |
| 入口代理 | — | 无（只有 `port-forward`） | **缺** |
| `node-controller` | 独立单元 | MegaCube Compose | 有，但作业路径用的是手工容器 |
| 推理引擎 | 独立单元 | 手工 `docker start` | **未由控制器托管** |

### 1.1 现状复核（2026-11-21 实测）

上表写于集群拆除期间，随后 `deploy/cluster/app-plane/` 已实际装配。`kubectl -n speechnexus
get pods` 当前 **10 个 Running**：

| 已运行 | 说明 |
| --- | --- |
| `postgres-0`、`minio-0` | StatefulSet，各 1/1 |
| `api`、`workbench-api` | 两个业务入口 |
| `workbench`、`admin` | 前端与控制台 |
| `asset-inspector` | 音频沙箱检查 |
| `admission-watch`、`artifact-retention` | 后台单元 |
| `bridge` | SSH 隧道（开发期替代内网 TLS） |

与上表相比，**已由缺转为一致**：`workbench-api`、`workbench-migrate`、`admin` 与 `workbench`
前端、`admission-watch`、`artifact-retention`。`migrate` 与 `minio-init` 以 Job 形式跑完即退出，
`get jobs` 已为空属正常。

仍缺：

| 单元 | 现状 | 备注 |
| --- | --- | --- |
| `binding-disable-worker` | 已定义、未启用 | `binding-disable` profile 按需启用 |
| `engine-readiness` / `engine-readiness-renew` | 已定义、未启用 | 依赖引擎登记 |
| `job-dispatch-worker` | 在 MegaCube，与网关同置 | 与参考 Compose 的摆放一致 |
| `gateway`、`node-controller` | 在 MegaCube | **同构缺口**：Mac 侧未部署，见 ADR 0014 |
| 推理引擎 | MegaCube 手工 `docker start` | 尚未由控制器托管 |

另有四类非单元级差异，都会在真实研发中咬人：

1. **健康检查与资源约束**：参考 Compose 的 `healthcheck`、`read_only`、`tmpfs`、`security_opt`
   在 k8s 侧只部分落实，副本重启时行为不同。
2. **环境变量**：参考 Compose 为每个单元声明了完整变量集，k8s 侧按「够跑通」裁剪，缺项在
   具体功能触发时才暴露。
3. **内部链路**：参考用内网 TLS 直连，双机用 SSH 隧道；延迟与超时预算不同（已实测：3 s 必超时）。
4. **身份与数据库角色**：工作台相关角色已建但无授权目标，Web 面接入前必须补齐。

## 2. 阶段

### 阶段 A：应用面单元补齐（本轮）

- 新增 `binding-disable-worker`、`artifact-retention`、`admission-watch` 三个 Deployment，
  以及 `engine-readiness` / `engine-readiness-renew` 两个 Job。
- 为它们渲染配置：`binding-disable-env`、`artifact-retention-env`、`admission-watch-env`、
  `readiness-env`，键名与参考 Compose 一致。
- 删除应用面里那份与参考布局矛盾的 `app-plane/job-dispatch.yaml`（派发器与网关同置）。
- 一致性检查脚本 `deploy/cluster/check-parity.py` 进入仓库，作为阶段 A 的判据。

当前落地：

- `deploy/cluster/app-plane/workers.yaml`：`binding-disable-worker`、`artifact-retention`、
  `admission-watch` 三个 Deployment，`engine-readiness`、`engine-readiness-renew` 两个 Job。
- `render-config.py` 新增四个 Secret：`binding-disable-env`、`artifact-retention-env`、
  `admission-watch-env`、`readiness-env`，键名与参考 Compose 一致。
- `api-env` 补齐参考 API 声明的八个只读/受控身份（catalog、audit、pool、quarantine release、
  settlement、workspace writer），此前这些变量在端口里完全缺失。
- `job-dispatch` 补 `SPEECHNEXUS_JOB_GATE_PRIVATE_DIR`。
- `deploy/cluster/check-parity.py`：参考 Compose 每个单元 → 端口去向的映射表；`ported`
  单元逐个核对变量、`planned` 单元必须写阶段理由；`--selftest` 用「删掉一个变量」「加一个未映射
  单元」「去掉阶段理由」三种注入证明检查会红。

**尚未验证**：以上清单尚未在运行中的集群里 `kubectl apply`，集群当前处于拆除状态；下一次装配
时一并验证。

### 阶段 A'：把算力面纳入版本管理（已完成，env 插值方案）

原先 `.runtime/cluster/compute/compose.yaml` 是未入库的运行态文件，内嵌 DSN 与口令，
是目前唯一「跑起来但仓库里没有」的装配单元。

- `deploy/cluster/compute/compose.yaml`：版本化的唯一来源，文件里只有 `${VAR}`，凭据全在环境变量里。
- `deploy/cluster/init-env.py` 写出 `.runtime/cluster/compute.env`：把数据库 URL 从集群内 Service
  改写到隧道端点（`127.0.0.1:15440`），并带上镜像标签、VAD 路径与桶名。
- 上机时 `docker compose --env-file compute.env up -d`，插值由 Compose 自己做，没有渲染脚本。
- `deploy/cluster/compute/{approved-templates,gateway-file-asr}.json`：无凭据的静态配置入库。
- 端点文件仍由 `publish-binding.py` 拥有（含已发布修订与引擎令牌）。

验证方式：`docker compose --env-file .runtime/cluster/compute.env config` 能解析出全部值，
隧道化 DSN 与镜像标签符合预期；`check-parity.py` 只读仓库内文件即可覆盖网关与派发器的全部变量。

### 阶段 B：控制器托管引擎与作业路径（已完成）

实机结果：

- 算力面按 `compute.env` 起在 MegaCube：`config-init`、`gateway`、`controller`、`dispatch`、
  `binding-disable-worker` 五个容器就绪；`gateway /healthz` 200，`controller /healthz` 200。
- 绑定重新发布：`mbrev-163aee31383944bf993ee305a5258845`（临时空库），端点文件已改绑。
- 控制器 `prepare` 成功：`intent_state=succeeded`、`observed_instance_state=ready`，
  容器 `speechnexus-instance-instance-asr-1-g1` 为 `running`。

**卡点（结构性，非配置笔误）**：控制器创建的容器**不发布端口**。模板 schema 里没有端口字段，
运行时也不做 `PortBindings`。于是：

| 探测点 | 结果 |
| --- | --- |
| 容器 IP `172.17.0.6:18190/health`（宿主机直连） | **200** |
| `127.0.0.1:18190/health` | 连接被拒 |

而绑定端点与桥接隧道都指向 `127.0.0.1:18190`，所以 `engine-readiness-renew` 报
`engine_unreachable`。参考装配里网关与引擎同处一个容器网络，因此没有这个缺口；本端口为了
隧道把网关放在宿主网络，缺口就暴露出来了。

修法（已实施，列为具名差异）：`deploy/cluster/compute/engine-port-shim.py` 在宿主网络监听
`127.0.0.1:18190`，把连接转发到引擎容器地址；作为 Compose 服务 `engine-shim` 运行，
地址来自 `compute.env` 的 `ENGINE_ADDRESS`，每次 `prepare` 后按实际容器地址重放。
这是对真实缺口的开发适配，不是产品组件——生产侧需要给模板补端口声明。

**完整链路实机验证**（全部一次通过）：

| 步骤 | 结果 |
| --- | --- |
| `prepare` | `succeeded` / `ready`，容器 `speechnexus-instance-instance-asr-1-g1` |
| 注册与就绪续期 | Job `Complete 1/1`（`--renew-readiness`） |
| 派发与执行 | `job-0eca…` 从 `queued` → `running` → `succeeded` |
| 制品校验 | 下载文件 sha256 `da4d6eb2…` 与响应一致，转写 `The quick brown fox jumps over the lazy dog.` |
| 容量归还 | `held_slots=0`、`held_attempts=0`、租约 `released` 且 `budget_released_at_us` 非空 |
| `stop` | `succeeded` / `stopped`，停止证据齐全（容器已退出、资源已释放、`exit_code=0`、容器 id、`verified_at`） |

修法实施中还修掉一处陈旧配置：`compose.env` 里 `SPEECHNEXUS_ENGINE_HEALTH_URL` 仍是上一轮手工
试验留下的 `http://host.docker.internal:18100`，Pod 内不可达，已改为
`http://bridge.speechnexus.svc:18190/health`。

### 阶段 C：Web 面（部分完成）

| 单元 | 状态 | 证据 |
| --- | --- | --- |
| `workbench-migrate` | 完成 | Job `Complete 1/1`：`bootstrapped workbench schema workbench at revision 11`，并把 runtime/cleanup 两个表面授予 `sn_workbench_runtime` / `sn_workbench_cleanup` |
| `workbench-api` | 完成 | `1/1 Running`，`/v1/workbench/status` 200（`workbench_enabled=true`、`llm_enabled=false`） |
| `workbench` 前端、`admin` | 已部署 | 镜像 `speechnexus-workbench-web:203d412b`、`speechnexus-admin-web:203d412b` 构建成功；两个 Pod `1/1 Running`，前端首页 200 |

构建备忘：`web.Dockerfile` 的两个基础镜像在 daocloud 镜像站没有白名单（`nginxinc/nginx-unprivileged` 直接 403），
需先本地 `docker pull` 两个基础镜像再构建，npm 走 `NPM_REGISTRY=https://registry.npmmirror.com`。

**代理已打通**。根因不是 Host 头，也不是权限：这个集群的 DNS 把 `*.svc` 解析成 Docker Desktop
的合成地址（`2001:2::476` 这类），nginx 连不上，`api` 与 `workbench-api` 两条上游都是 502
（早先 `/readyz` 的 200 是静态兜底，属假阳性）。

修法：上游用 IPv4 ClusterIP 的静态 `proxy_pass`。**这些地址不写进仓库**——它们是集群分配的
环境事实，重建后会变，所以：

- 模板 `deploy/cluster/app-plane/workbench-nginx.conf.template` 只保留 `${WEB_SPEECH_UPSTREAM}`
  与 `${WEB_WORKBENCH_UPSTREAM}` 占位（`envsubst` 在容器启动时替换成字面 URL，nginx 不会二次解析）。
- `deploy/cluster/app-plane/render-web-upstreams.py` 从**运行中的 Service** 读取 ClusterIP
  与集群 DNS 地址，生成 ConfigMap `web-upstreams`；两个前端 `envFrom` 消费它。
- 重建 `api` 或 `workbench-api` 后重跑该脚本即可，仓库里没有任何地址。

一致性检查新增一条规则：提交树里出现集群地址段（`10.96.*`、`172.17.*`、`10.244.*`）就判红，
`--selftest` 注入 `10.96.7.7` 证明它真的会红。同时把 `engine-port-shim` 的
`ENGINE_ADDRESS` 默认值去掉了——猜一个地址比报错更糟，缺值现在由 Compose 的
`${ENGINE_ADDRESS:?…}` 明确拒绝。

**`planned` 已归零**：参考 Compose 的 18 个服务全部映射为 `ported` 或 `equiv`。

### 实机验证记录（应用面）

在全新的空库上按装配顺序起来，逐个核验端口单元：

| 单元 | 结果 | 证据 |
| --- | --- | --- |
| `postgres` / `migrate` / `minio` / `minio-init` | 就绪 | Pod `1/1 Running`，Job `Completed` |
| `admission-watch` | 就绪 | `1/1 Running`；补齐 `sn_job_admission` 的 Job 角色后才连得上 |
| `artifact-retention` | 就绪 | `1/1 Running`，日志有 `artifact_retention_pass` 周期事件 |
| `api` | 就绪 | `1/1 Running`，`/readyz` 200 |
| `bridge` | 就绪 | `1/1 Running`，MegaCube `authorized_keys` 已含桥接公钥 |

装配期发现并修掉三个真实缺陷，都不是配置笔误：

1. **未声明的环境变量会让进程直接拒绝启动**。`api-env` 里曾出现
   `SPEECHNEXUS_CATALOG_READER_DATABASE_URL`，参考实现里没有这个键
   （对应的是 `SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL`）。设置模型对
   `SPEECHNEXUS_` 前缀的未知键采取整体拒绝，不忽略。
2. **`Secret` 卷的文件是 `..data` 符号链接**，而配方加载器用 `O_NOFOLLOW` 打开，
   直接得到 `ELOOP`；改成 `subPath` 又能绕过符号链接，却换来第三个问题。
3. **`Secret` 文件的归属与权限不满足加载器**：加载器要求文件属主等于进程 uid，且
   组/其他位全空。`subPath` 加 `fsGroup` 只能得到 `0440 root:10001`，两个条件都不满足。

最终做法是把配方经 init 容器（同镜像、非 root）`install -m 0400` 暂存到内存 `emptyDir`，
主容器从那里读；`api.yaml` 已按此实现。任何要读私有配方的单元都需要同样的暂存步骤——
`readiness-renew` Job 仍直接挂载 `Secret`，尚未实机验证。

### 阶段 A 补齐：两个停用角色（已完成）

`init-env.py` 现在为 `sn_disable_scan` / `sn_disable_settle` 生成口令并回填两个 DSN；
`deploy/cluster/provision-roles.py`（已入库）创建这两个角色并安装
`disable_coordinator_grants`、`disable_settlement_grants`、`binding_disable_admission_grants(sn_api)`。

实机核验：两个角色都在库里（`pg_roles` 命中），各自用 `compose.env` 的口令登录成功，
表级授权分别为 4 条与 6 条，`speech` schema 的 USAGE 为真。

停用 worker 的落点也跟着改了：它的运行时拒绝「非 loopback 的明文 HTTP 网关」，所以与派发器
一样放在算力面、紧邻网关（`http://127.0.0.1:8010`），与参考 Compose 的摆放一致。

实机核验：`admission-watch` 在补齐 `sn_job_admission` 的 Job 角色后为 `1/1 Running`。

### 阶段 B：控制器托管的引擎与作业路径

- 引擎由控制器 `prepare` 拉起（模板绑定 `0.0.0.0`，桥接网络内不发布端口），而不是手工容器。
- 注册与网关端点指向该实例；一次公开文件 ASR Job 走通，结束后用 `stop` 收尾并留下停止证据。
- 引擎镜像、模板、`--host` 与显存参数写进模板文件，不再散落在命令历史里。

验收：Job `succeeded`、制品摘要一致、租约与预算归还，且该次执行的实例由控制器创建与销毁。

### 阶段 C：Web 面与入口代理

- `workbench-migrate` → `workbench-api` → `workbench` 前端 + `admin`，与 Speech API 同源路由。
- 入口代理在 k8s 内提供单一 Origin（开发期可用自签证书），转发 `/`、`/admin`、`/workbench`。
- 工作台角色授权目标补齐，`verify-roles.py` 从 `login only` 变为实际关系探针。

验收：浏览器经单一 Origin 打开工作台与 Admin，Speech API 的公开路由不回归；工作台迁移可重复执行。

### 阶段 D：把差异固化

- `check-parity.py` 的 `planned` 归零，只保留具名白名单（每条必须写原因）。
- 在 CI 加一条只读检查，与 `check-doc-statements.py` 同级。
- 运行手册补「与参考 Compose 的差异清单」一节。

## 3. 差异白名单（必须具名）

允许长期存在的差异，每条都要有原因：

| 差异 | 原因 |
| --- | --- |
| 跨主机链路用 SSH 隧道而非内网 TLS | 两台主机之间没有可管理的私有网络；隧道在桥接 Pod 内，边界等价 |
| 应用面在 Docker Desktop k8s、算力面在 Compose | 硬件异构：GPU 只在 MegaCube，开发机不装 CUDA |
| 单节点、单实例、单槽 | 规模最小化，拓扑关系保持 |

白名单之外的差异一律视为缺陷，由 `check-parity.py` 判红。