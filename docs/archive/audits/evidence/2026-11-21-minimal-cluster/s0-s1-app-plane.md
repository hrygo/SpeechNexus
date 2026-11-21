# 最小同构集群 S0–S1：应用面 k8s 装配证据

- 采集时间：2026-11-21 00:00–00:20（Asia/Shanghai）
- 范围：S0（版本固定与基线）与 S1（应用面装配），对应实施卡 #1085
- 环境：本机 Docker Desktop k8s `desktop-control-plane` v1.36.4；MegaCube 本阶段未参与

## S0 固定版本

| 项 | 值 | 复核命令 |
| --- | --- | --- |
| 仓库 | `203d412b` | `git log -1 --format=%h` |
| 应用镜像 | `speechnexus-python:203d412b`，`arm64`，`sha256:706184f35091…` | `docker image inspect` |
| 对象存储镜像 | `speechnexus-minio:RELEASE.2025-09-07T16-13-09Z`，`arm64` | `docker image inspect` |
| PostgreSQL 镜像 | `postgres:16.15-alpine`，`arm64` | `docker image inspect` |
| 应用面镜像可见性 | 本地镜像无需 registry，Pod 直接运行 | 本次装配全程未推送镜像 |

镜像由仓库 Dockerfile 在 `203d412b` 上重建；未使用 `sn534-*` 旧标签。

## S1 装配结果

```text
$ kubectl -n speechnexus get pods,jobs
pod/api-65c5c7b84d-5ttgs   1/1 Running
pod/migrate-rmncc          0/1 Completed
pod/minio-0                1/1 Running
pod/minio-init-fhwns       0/1 Completed
pod/postgres-0             1/1 Running
job.batch/migrate          Complete 1/1
job.batch/minio-init       Complete 1/1
```

- 迁移：`speech.alembic_version = 0063_deletion_targets_blocked`，`speech` schema 84 张表。
- 对象存储：`bucket ready: speechnexus-results (versioning enabled)`。
- API：`GET /readyz` → `200`，`checks` 中 `postgresql` 与 `object_storage` 均为 `ok`；
  `GET /v1/nexus/version` → `contract_digest=sha256:6728dce6…`。
- 角色：`pg_roles` 中 `sn_%` 共 23 个——20 个基线身份（§6 脚本原样执行；脚本打印 19 个，
  第 20 个 `sn_qualification_reader` 只服务宿主侧审计命令，故不在打印列表）+ 3 个 Job 角色
  （`provision-job-roles.py`，249 条 GRANT）。`sn_profile_writer` 未建：它要求与部署方资格文档
  同时启用，本阶段不覆盖。
- 逐身份探针：`deploy/cluster/app-plane/verify-roles.py` 让 23 个身份各自连接并查询一条自己
  被授权的关系，全部成功；`sn_workbench_runtime` / `sn_workbench_cleanup` 只验证登录（授权目标
  在工作台命名空间，本阶段未建）。
- Secret/ConfigMap：`api-env`、`postgres-credentials`、`minio-credentials`、
  `migrate-credentials`、`private-config`、`minio-init-script`；全部由
  `deploy/cluster/app-plane/render-config.py` 从 `.runtime/cluster/compose.env` 渲染，
  未手工抄写。

## 反例（注入后确实变红）

| 注入 | 观测 | 说明 |
| --- | --- | --- |
| `SPEECHNEXUS_DATABASE_URL` 指向不存在的库 | `Application startup failed. Exiting.`，`psycopg_pool.PoolTimeout`，Pod `0/1 Error`，端口 8000 拒绝连接 | 启动即拒绝，不是降级继续服务 |
| `REVOKE SELECT ON speech.speech_jobs FROM sn_job_writer` | `GET /v1/nexus/jobs?limit=1` 由 `200` 变 `503 job_unavailable`；同一时刻 `sn_job_admission` 自己的查询仍成功 | 撤权影响面止于该身份 |
| 同上，观察 readiness | 撤权期间 `/readyz` 仍为 `200` | readiness 不覆盖 Job 读面；判据由端点本身承担 |
| 删除 `postgres-0` 并等待重建 | 迁移 head 与 84 张表不变；API 进程存活，首个请求丢弃 1 条失效连接后恢复 `200` | PVC 持久化成立；池会丢弃坏连接 |

`/v1/nexus/jobs` 的失败来自真实 Pod 与真实 PG，不是 mock。

## 已知缺口（不影响 S1 判定，留给后续卡）

- 同步转写/同步语音未启用：日志提示缺少 `SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL`；
  文件 ASR 走异步 Job，不依赖该身份。
- 配方为 `deploy/private/*.example.json` 占位，`binding_revision_id` 等尚未指向真实发布，
  S5 发布 binding 后替换。
- `/readyz` 不覆盖 Job 读面与派发面；后续卡以端点级证据补充，不靠 readiness 代替。
