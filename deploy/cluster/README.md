# 最小同构集群装配

本目录承载「MegaCube 算力面 + 本机 k8s 应用面」的可执行装配。目标、切片与验收口径见
[最小同构集群实施规划](../plans/2026-11-20-minimal-isomorphic-cluster.md)；本文件只写实际操作。

## 拓扑与端口

| 单元 | 位置 | 访问地址 |
| --- | --- | --- |
| PostgreSQL | k8s `speechnexus` | `postgres.speechnexus.svc:5432` |
| 对象存储 | k8s `speechnexus` | `minio.speechnexus.svc:9000` |
| Speech API | k8s `speechnexus` | `api.speechnexus.svc:8000` |
| 桥接 | k8s `speechnexus` | `bridge.speechnexus.svc:18010` / `:18020` |
| 执行网关 | MegaCube | `127.0.0.1:8010`（经隧道 `:18010`） |
| 节点控制器 | MegaCube | `127.0.0.1:8022`（经隧道 `:18020`） |
| 引擎实例 | MegaCube | `127.0.0.1:18190` |

MegaCube 侧 `9000`、`15432`、`18100` 已被既有服务占用，隧道端口固定为
PG `15440`、S3 `19100`，不得改回常用值。

## 身份

| 项 | 取值 |
| --- | --- |
| `node_id`（应用面） | `node-app-1` |
| `node_id`（算力面） | `node-megacube-1` |
| `gateway_id` | `gateway-megacube-1` |
| `instance_id` / `generation` | `instance-asr-1` / `1` |
| `pool_id` | `asr-batch` |
| 工作区 | `workspace-default` |

## 前置

- 本机：Docker Desktop k8s 已启用；`kubectl` 指向 `docker-desktop` 上下文。
- 镜像：`speechnexus-python:<sha>` 与 `speechnexus-minio:RELEASE.2025-09-07T16-13-09Z`
  在本地 Docker 镜像库中（Pod 直接可见，无需 registry）。
- 配置：`.runtime/cluster/compose.env`（`init-env.py` 生成，含随机口令）与
  `.runtime/cluster/private/`（配方 JSON）。两者都不入库。
- 算力面：`deploy/cluster/compute/compose.yaml` 是版本化来源，文件里只有 `${VAR}`；
  `init-env.py` 另外写出 `.runtime/cluster/compute.env`（隧道化 DSN 与镜像标签），
  上机时 `scp` 两者并用 `docker compose --env-file compute.env up -d`，插值由 Compose 完成。

## 顺序

```bash
python3 deploy/cluster/init-env.py                      # 生成 .runtime/cluster/compose.env
python3 deploy/cluster/app-plane/render-config.py       # 渲染 Secret / ConfigMap
kubectl apply -f deploy/cluster/app-plane/namespace.yaml
kubectl apply -f deploy/cluster/app-plane/postgres.yaml
kubectl apply -f deploy/cluster/app-plane/minio.yaml
kubectl apply -f deploy/cluster/app-plane/minio-init.yaml
kubectl apply -f deploy/cluster/app-plane/migrate-job.yaml
kubectl apply -f deploy/cluster/app-plane/api.yaml
```

角色授权在迁移之后执行，SQL 以[最小 Compose 手册 §6](../../../docs/operations/compose-minimal.md#6-创建非-owner-运行角色与最小授权)
为准：把该节 `uv run --all-packages python - <<'PY'` 的代码块原样取出执行（不要在 k8s 侧另写一份
授权 SQL，否则同一规则就有了两个定义点），`SN_ADMIN_DSN` 指向 `127.0.0.1:<本地转发端口>`；
Job 三角色用 `scripts/dev-env/provision-job-roles.py`，三个 Job DSN 先写回 `compose.env`。

最后用 `verify-roles.py` 逐身份核验：每个角色用自己的 DSN 登录，并查询一条它确实被授权的
关系。工作台两个角色此时还没有授权目标（授权目标在工作台命名空间），探针只报 `login only`。

## 回退

```bash
kubectl delete namespace speechnexus
```

应用面全部对象都在该 namespace 内，删除即恢复；MegaCube 侧不受影响。

## 算力面控制器

`deploy/cluster/compute/controller-deployment.yaml` 把控制器作为常驻 k3s 工作负载运行，
取代 `compose.yaml` 里的 `controller` 服务。两者是同一组变量，区别只有三处，都是刻意的：

- `SPEECHNEXUS_CONTROLLER_RUNTIME=k8s`：驱动改为经 API server 建引擎 Pod
- `SPEECHNEXUS_CONTROLLER_HOST=0.0.0.0`：compose 用 host 网络可以绑环回，Pod 不行
- 不挂容器守护进程 socket：选了 k8s 驱动之后，那条路径没有任何调用方

清单里只有 `${CONTROLLER_IMAGE}`，与 `compose.yaml` 同一约定，插值来自 `versions.env`。
`component token`、`approved-templates.json` 都由下面两步从既有来源生成，不写进清单：

```bash
# 镜像必须先进入 k3s 的 containerd。二选一，都要求节点权限或一个内网 registry。
docker save speechnexus-node-controller:<sha> | sudo k3s ctr images import -
#   或者：docker push <registry>/speechnexus-node-controller:<sha>，再把清单里的
#   image 换成该地址

# 两个输入取自既有来源，不在清单里复制
kubectl -n speechnexus create configmap controller-approved-templates \
  --from-file=approved-templates.json=deploy/cluster/compute/approved-templates.json \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n speechnexus create secret generic controller-component-token \
  --from-literal=token="$(grep '^COMPONENT_TOKEN=' .runtime/cluster/compute.env | cut -d= -f2-)" \
  --dry-run=client -o yaml | kubectl apply -f -

CONTROLLER_IMAGE=$(grep '^CONTROLLER_IMAGE=' deploy/cluster/versions.env | cut -d= -f2) \
  envsubst < deploy/cluster/compute/controller-deployment.yaml | kubectl apply -f -
```

投产前先停掉 compose 的控制器，否则同一个 `node_id` 上有两个写入者抢同一份意图：

```bash
docker compose -p speechnexus-cluster-compute --env-file .runtime/cluster-compute.env \
  -f deploy/cluster/compute/compose.yaml stop controller
```

回退就是反过来：起 compose 控制器、删掉这个 Deployment。意图存储是 SQLite 单文件，
两侧不共享，切换时在途意图的归属只认最后写入的那一侧。

## 已知单点

桥接 Pod 是两台主机之间唯一通道；Mac 休眠或 Docker Desktop 重启会同时停掉 k8s 与隧道。
本拓扑用于功能验证，不是高可用形态。
