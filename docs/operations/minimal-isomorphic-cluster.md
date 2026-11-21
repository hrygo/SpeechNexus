# 最小同构集群运行手册

本手册描述如何在两台物理主机上装配并运行与生产同构的最小集群：应用面在本机 Docker Desktop
k8s，算力面在 MegaCube 上的 Docker Compose，两者经一个桥接 Pod 双向连通。它用于功能验证，
不是生产部署，也不构成 HA、实时链或生产 TLS 资格。

- 设计与验收：`docs/plans/2026-11-20-minimal-isomorphic-cluster.md`
- 实测证据：`docs/archive/audits/evidence/2026-11-21-minimal-cluster/`
- 运行手册的通用 Job 语义：`docs/operations/async-job-runbook.md`（本手册不重复）

## 1. 拓扑

| 位置 | 组件 | 形态 |
| --- | --- | --- |
| 本机 k8s（命名空间 `speechnexus`） | PostgreSQL、MinIO、迁移 Job、`api`、`asset-inspector`、`job-dispatch`、`bridge` | Deployment / StatefulSet / Job |
| MegaCube | `gateway`（8010）、`controller`（8022）、`dispatch`、`speechnexus-asr-1` 引擎（18190） | Docker Compose + 一个手工容器 |

桥接 Pod 用 SSH 隧道承载四个方向：`-L 18010→gateway:8010`、`-L 18020→controller:8022`、
`-L 18190→engine:18190`、`-R 15440→postgres:5432`、`-R 19100→minio:9000`。桥接 Pod 的
Service 必须列出每一个端口（含 `engine 18190`），否则 Pod 到 Service 的连接会超时。

## 2. 装配顺序

```bash
# 1. 应用面骨架
kubectl apply -f deploy/cluster/app-plane/postgres.yaml
kubectl apply -f deploy/cluster/app-plane/minio.yaml
python3 deploy/cluster/app-plane/render-config.py      # 生成全部 Secret 与 ConfigMap
kubectl apply -f deploy/cluster/app-plane/migrate-job.yaml
kubectl -n speechnexus wait --for=condition=complete job/migrate --timeout=300s

# 2. 数据库角色（两次，顺序不能反：先运行角色，再 Job 角色）
set -a; . .runtime/cluster/compose.env; set +a
SN_ADMIN_DSN=... python3 deploy/cluster/provision-roles.py
SN_ADMIN_DSN=... python3 scripts/dev-env/provision-job-roles.py

# 3. 应用面服务
kubectl apply -f deploy/cluster/app-plane/api.yaml
kubectl apply -f deploy/cluster/app-plane/asset-inspector.yaml
kubectl apply -f deploy/cluster/app-plane/job-dispatch.yaml
kubectl -n speechnexus rollout status deploy/api --timeout=180s
```

`render-config.py` 的输入是 `.runtime/cluster/compose.env`。它渲染七个 Secret：`postgres-credentials`、
`minio-credentials`、`migrate-credentials`、`api-env`、`registration-credentials`、
`asset-inspector-token`、`dispatch-env`，以及 `private-config` 与 `minio-init-script`。

### 2.1 发布 binding 与注册实例

```bash
uv run --offline --no-sync --all-packages python deploy/cluster/app-plane/publish-binding.py \
  --api http://127.0.0.1:18000 \
  --recipe .runtime/cluster/private/transcription-recipe.json \
  --gateway-endpoint .runtime/cluster/compute/gateway-file-asr-endpoint.json
kubectl apply -f deploy/cluster/app-plane/register-engine-job.yaml
```

注册在集群内执行，不在隧道上执行：注册事务的默认预算是 10 s，经 SSH 隧道必然超时。

### 2.2 算力面

```bash
scp deploy/cluster/compute/{compose.yaml,gateway-file-asr.json,approved-templates.json} \
  .runtime/cluster/compute.env .runtime/cluster/compute/gateway-file-asr-endpoint.json \
  megacube:~/speechnexus-cluster-compute/
ssh megacube 'cd ~/speechnexus-cluster-compute && docker compose --env-file compute.env up -d'
```

`config-init` 先把私有配置以 `10001:10001 0600` 安装进卷，`gateway` 与 `dispatch` 依赖它完成。
网关的 `vad_tuning.config_revision` 必须等于 binding 的 `policy_revision_id`，否则每条命令都会被判
「命令与绑定不匹配」。

## 2.5 渲染 Web 前端的上游地址

两个前端经 nginx 反代到 `api` 与 `workbench-api`。上游地址是集群分配的 ClusterIP，
不写进仓库，由脚本从运行中的 Service 读取后生成 ConfigMap：

```bash
python3 deploy/cluster/app-plane/render-web-upstreams.py
kubectl -n speechnexus rollout restart deploy/workbench deploy/admin
```

`api` 或 `workbench-api` 被重建（ClusterIP 变化）后必须重跑，否则前端会 502。

## 3. 验证一次 Job

```bash
curl -s -X POST http://127.0.0.1:18000/v1/nexus/jobs \
  -H "Idempotency-Key: $(uuidgen | tr 'A-Z' 'a-z')" -H 'content-type: application/json' \
  -d '{"kind":"asr","model":"model-asr-minimal","input":{"audio_asset_id":"<asset-id>"}}'
curl -s http://127.0.0.1:18000/v1/nexus/jobs/<job-id>
curl -s -o /tmp/transcript.json http://127.0.0.1:18000/v1/nexus/artifacts/<artifact-id>/content
shasum -a 256 /tmp/transcript.json      # 必须等于响应里的 artifacts[].sha256
```

成功判据不止 `succeeded`：还要 `engine_instances.held_slots` 与 `workspace_quotas.held_attempts`
回到 0、`admission_leases` 为 `released/complete` 且 `budget_released_at_us` 非空。

## 4. 故障演练的预期

| 故障 | 预期 | 实测结论 |
| --- | --- | --- |
| API 重启 | 在途 Job 不受影响，释放照旧 | 通过 |
| 网关重启 | 在途执行判 `unknown_quarantined`，不发布制品，容量不归还 | 通过（无自动归还） |
| 执行中杀引擎 | 同上，`engine_transport_unknown` | 通过（无自动归还） |
| PG 短暂不可用 | 已派发的 Job 仍可完成并释放 | 通过 |
| 桥接中断 | 执行被切断，Job 停在 `running`，容量不归还 | 通过（无自动归还） |

隔离后的容量只能经运行手册 §13.1 的操作员出口归还；这不是本集群的缺陷，而是默认部署的既定行为。

## 5. 拆除

```bash
deploy/cluster/teardown.sh --compute-host megacube            # 先 --dry-run 看范围
```

脚本只删除 `speechnexus` 命名空间、`speechnexus-cluster-compute` Compose 项目（含卷）、
`speechnexus-asr-1` 容器与项目目录，并在结束时校验这三者都不存在、其余容器逐条列出但不动。
引擎以 root 写入的 `triton-cache/` 无法用本账号删除，脚本会把它作为残留报告出来；
主机管理员执行 `sudo rm -rf ~/speechnexus-cluster-compute` 即可清掉，其中没有凭据。

## 6. 已知限制

- 单引擎、单实例、单槽；一次隔离就换走唯一槽位。
- 隧道化数据库要求放大超时预算：网关 `CONTROL_TIMEOUT_SECONDS` 与派发器
  `JOB_DISPATCH_STORAGE_TIMEOUT_SECONDS` 都不能用默认值（3 s 会在隧道上超时）。
- 无 TLS、无 HA、无实时链；`G5` 保持开启，本集群不构成其关闭证据。
- 引擎容器手工管理，控制器的 `prepare`/`stop` 生命周期尚未接入本集群的作业路径。