---
title: "最小同构集群实施规划：MegaCube 算力面 + 本机 k8s 应用面"
status: active
version: "0.1.0"
date: 2026-11-20
---

# 最小同构集群实施规划

> 本文是实施计划，保留目标、决定依据与执行顺序；完成状态以实际运行证据和[交付跟踪矩阵](delivery-tracking.md)为准，不以本文勾选代替验收。

**目标**：用两台真实物理主机跑通「公开契约 → 应用/控制面 → 执行网关 → 真实引擎 → 对象存储」的文件语音完整链，拓扑与生产同构、规模最小，并把跨主机身份、提交屏障、停止证据作为验收对象。

**架构**：应用面（PostgreSQL、对象存储、Speech API、Job 派发）运行在本机 Docker Desktop 的 k8s 单节点；算力面（执行网关、节点控制器、vLLM 引擎）运行在 MegaCube 的 Docker 上；两主机经 k8s 内的 SSH 桥接 Pod 双向连通，不使用公网入站端口。

**规格依据**：[PRD O-PHYSICAL](../prd.md#o-physical)、[部署单元规划](../architecture/deployment-units.md)、[ADR 0011](../decisions/0011-distributed-deployment-and-platform-boundaries.md)、[ADR 0004](../decisions/0004-cluster-ready-architecture-and-single-node-mvp.md)、[技术方案](../architecture/technical-blueprint.md)。

## 1. 范围

### 1.1 「同构」的判定口径

同时满足以下七条才算同构验证，缺一条按对应缺口登记：

1. **单元边界不变**：业务/控制面与算力面分处不同主机，API/工作台不持有 GPU、Docker socket 或引擎凭据。
2. **公开契约不变**：仍由同一份 `speech-openapi.yaml` 与标准 SDK 消费，不为分机新增路由或扩展字段。
3. **持久权威唯一**：PostgreSQL 只在应用面；算力面不写第二套协调权威，节点本地记录仍只承担节点范围职责。
4. **身份与代次不变**：`host_node_id` / `gateway_id` / `instance_id` / `generation` 在跨主机后仍显式配置并核对。
5. **提交屏障与停止证据跨主机成立**：先持久化再外发；停止以执行层与资源层双证据判定，HTTP 断连不算停止。
6. **内容经对象存储与网络协议传递**：不使用共享宿主目录、本地文件描述符或跨容器共享卷传递业务内容。
7. **故障按原身份恢复**：网关、隧道、引擎、数据库各自重启后不重复发布、不提前释放预算、不补发新身份。

### 1.2 明确不证明的事项

本拓扑是验证形态，不是发布资格。以下各项仍须独立验收，不得由本次结果推定：

- 高可用、RPO / RTO、备用 GPU 接管与节点丢失切换。
- 实时链的首包延迟（p95 ≤ 1000 ms / 800 ms）与浏览器全矩阵。
- 生产 TLS/PKI、入口认证、多租户与限流。
- 「内容不出部署网络」——本拓扑经公网 SSH 隧道传输，是受控例外，见 §8。
- G5 发布关口与 D1 平台关口；本次最多登记为 O-PHYSICAL 的物理分机证据。

### 1.3 首轮不做

Workbench 与 AdminUI、实时网关、多副本、资产检查容器（保持 `text` 装配）、音色设计/克隆车道、备用节点。

## 2. 现状（已核实）

| 对象 | 事实 | 核实方式 / 时间 |
| --- | --- | --- |
| 本机 k8s | Docker Desktop k8s `v1.36.4`，单节点 `desktop-control-plane`（arm64 LinuxKit），allocatable `4` CPU / `15.6 GiB`，StorageClass `standard`(`local-path`)，无 IngressClass | `kubectl get nodes -o jsonpath`、`kubectl get sc`、`kubectl get ingressclass`，2026-11-20 |
| 镜像可见性 | 本机构建的 `speechnexus-python` 镜像打 tag 后 Pod 直接进入 `Running`，无需 registry 或拉取 | `docker tag` + `kubectl run` + `get pod -o jsonpath`，2026-11-20 |
| Pod 出站 | Pod 内 TCP 可达 MegaCube 公网入口 `47.103.22.160:6000`，DNS 与默认路由正常 | Pod 内 `/dev/tcp` 探测，2026-11-20 |
| MegaCube | DGX Spark / GB10，`aarch64`，20 CPU，统一内存 121 GiB（当前可用 93 GiB；35B LLM 容器已退出 3 小时），根分区可用 2.9 TB | `ssh megacube` 只读，2026-11-20 |
| MegaCube 无容器编排 | `k3s` / `kubectl` / `kubelet` 均不存在 | `which`，2026-11-20 |
| MegaCube 制品 | `~/speechnexus-models` 20 GB：ASR 0.6B/1.7B、TTS Base/CustomVoice/VoiceDesign、ForcedAligner、Diarization、Silero VAD；镜像 `speechnexus-python`、`speechnexus-admin`、`speechnexus-workbench`、`speechnexus-asset-inspector`（标签 `sn534-*`）；`mysql:8.4` 协调容器 | `ls`、`docker images`，2026-11-20 |
| MegaCube 既有单机栈 | 用户级 `speechnexus-postgres`(55432)、`speechnexus-minio`(59000) 仍在运行，属上一轮单机引导，本规划不改动 | `systemctl --user`，2026-11-20 |
| MegaCube 端口占用 | `9000`、`15432`、`18100` 等已被占用；`8010`、`8022`、`18190`、`15440`、`19100` 空闲 | `ss -ltn` + loopback 连接探测，2026-11-20 |
| 部署制品 | `deploy/compose.yaml` 有 `gateway`、`api`、`job-dispatch`、`migrate` 等 profile，**没有 `node-controller`**；引擎模板与控制器镜像文档见 [node-controller 镜像说明](../../deploy/images/node-controller.md) | 读 `deploy/compose.yaml`、`deploy/README.md` |
| 历史真实执行 | MegaCube 单机以六个授权窗口推进公开 SDK 同步 TTS（固定声音、单实例、短英文）；第五、六窗口取得 SDK 成功、可信停止与预算释放，第六窗口补齐完整关联，见[受控 TTS 验收](../archive/audits/2026-11-03-controlled-tts-prepare.md) | 文档，非本次实测 |
| 代码基线 | `main` 至 `203d412b` | `git log -1` |
| 应用面 k8s 装配（S1 已完成） | namespace `speechnexus`：PG 16.15 迁移至 `0063_deletion_targets_blocked`（84 表）、MinIO 桶 `speechnexus-results` 版本化开启、API `/readyz` 200（PG 与对象存储 `ok`）、23 个非 owner 角色（20 基线 + 3 Job） | [S0–S1 证据](../archive/audits/evidence/2026-11-21-minimal-cluster/s0-s1-app-plane.md)，2026-11-21 |
| 桥接（S2 已完成） | k8s 内桥接 Pod 经单条 SSH 建立双向通道：MegaCube 仅新增 `127.0.0.1:15440`/`127.0.0.1:19100` 两个 loopback 监听；从 MegaCube 经隧道读 MinIO 健康检查返回 200，以 `sn_job_writer` 身份查询 PG 成功；删除授权行后 `Permission denied (publickey)`、Pod 0/1，恢复后 10 秒内 Ready | [S2 证据](../archive/audits/evidence/2026-11-21-minimal-cluster/s2-bridge.md)，2026-11-21 |
| 算力面网关与控制器（S3 已完成） | MegaCube 上 `speechnexus-python:203d412b` 与 `speechnexus-node-controller:203d412b` 运行：网关 `/readyz` 200（`control_postgresql` 经隧道 ok）、控制器 `/readyz` 200（`container_daemon` 需 `group_add: 988`）；监听仅 `127.0.0.1:8010`/`:8022` | [S3–S4 证据](../archive/audits/evidence/2026-11-21-minimal-cluster/s3-s4-compute-plane.md)，2026-11-21 |
| 算力面引擎（S4 部分） | `qwen3-asr-1.7b` 在 `127.0.0.1:18190` 服务，`/health` 200、`/v1/models` 正常、容器 4.0 GiB/12 GiB；但为手工 `docker run`，无控制器 prepare→READY→stop 生命周期 | 同上，2026-11-21 |
| 应用面反例 | 错误 `DATABASE_URL` 使 API 启动即退出（`PoolTimeout`）；撤 `sn_job_writer` 对 `speech_jobs` 的 `SELECT` 后 `/v1/nexus/jobs` 200→503，同库 `sn_job_admission` 不受影响；该撤权下 `/readyz` 仍 200；删 `postgres-0` 后迁移头与 84 表不变 | 同上，2026-11-21 |

结论：**S0–S8 全部完成**。S5 的首个跨主机文件 ASR Job 到达 `succeeded` 且制品 sha256 与记录一致（`s5-first-success.md`）；S6 的租约与预算按停止证据归还，首例手工补做、第二例为自动路径（`s6-release.md`）；S7 的五类故障各演练一次，无重复发布、无提前释放（`s7-fault-drills.md`）；S8 拆除后命名空间与算力面容器/卷全部移除，生产容器名称与镜像逐条比对无差异（`s8-teardown.md`），运行手册见 `docs/operations/minimal-isomorphic-cluster.md`。S4 的控制器生命周期已走通 `prepare → ready → stop`，停止证据含容器退出、资源释放与退出码 （`s4-controller-prepare.md`）；关键是模板必须把引擎绑定到 `0.0.0.0`，否则控制器探测不到。遗留：作业路径仍用人工启动的引擎与 `127.0.0.1:18190`，尚未改为控制器托管的实例；`unknown_quarantined` 之后没有自动归还路径。

## 3. 目标拓扑

| 部署单元 | 主机 | 形态 | 关键依赖 | 访问地址 |
| --- | --- | --- | --- | --- |
| PostgreSQL | 本机 k8s | StatefulSet，1 副本，`standard` PV | — | `postgres.<ns>.svc:5432` |
| 对象存储 | 本机 k8s | StatefulSet，1 副本，PV | — | `minio.<ns>.svc:9000` |
| 迁移 / 授权 | 本机 k8s | Job（一次性） | PG | 一次性 |
| Speech API | 本机 k8s | Deployment，1 副本 | PG、S3、网关 | `api.<ns>.svc:8000` |
| Job 派发 | 本机 k8s | Deployment，1 副本 | PG、S3、网关 | 出站至桥接 |
| 桥接 | 本机 k8s | Deployment，1 副本 + ClusterIP | MegaCube SSH | `bridge.<ns>.svc:18010/18020` |
| 执行网关 | MegaCube | 容器，`--network host`，仅 loopback 监听 | PG、S3（经隧道）、引擎 | `127.0.0.1:8010` |
| 节点控制器 | MegaCube | 容器，`--network host`，挂 Docker socket | Docker、本地 SQLite | `127.0.0.1:8022` |
| 引擎实例 | MegaCube | 容器（控制器模板或手工） | GPU、只读模型 | `127.0.0.1:18190` |

```mermaid
flowchart LR
    SDK["标准 SDK / curl"] -->|"NodePort/port-forward"| API["Speech API (k8s)"]
    API --> PG[("PostgreSQL (k8s)")]
    API --> S3[("MinIO (k8s)")]
    API -->|"bridge:18010"| BR["桥接 Pod (k8s)"]
    DISP["Job 派发 (k8s)"] --> PG
    DISP --> S3
    DISP -->|"bridge:18010"| BR
    BR -->|"SSH 隧道 -L"| GW["执行网关 (MegaCube)"]
    GW -->|"隧道 -R :15440"| PG
    GW -->|"隧道 -R :19100"| S3
    GW -->|"127.0.0.1:18190"| ENG["引擎实例 (MegaCube)"]
    CTRL["节点控制器 (MegaCube)"] -->|"Docker"| ENG
```

## 4. 关键设计决定

| 编号 | 决定 | 依据与被否决方案 |
| --- | --- | --- |
| D1 | 应用面用现有 Docker Desktop k8s 单节点，算力面用 MegaCube 上的 Docker + `node-controller`，**不在 MegaCube 装 k3s** | 节点控制器的适配器就是 Docker daemon；装 k3s 会与 kubelet 争容器管理权，且改动生产主机的编排栈。被否决：MegaCube 跑 k3s（改动大、无收益）、继续单机 Compose（不构成物理分机） |
| D2 | 跨主机连通性由 k8s 内的**桥接 Pod 维持一条 SSH 连接**：`-L` 暴露网关/控制器，`-R` 把 PG / S3 暴露到 MegaCube 的 loopback | Pod 出站已验证可达；不需要公网入站、不改 MegaCube 网络栈、可整体删除。被否决：公网暴露 8010/5432（内容与凭据越界）、WireGuard / Tailscale（更接近生产，但要在生产主机装网络组件，留到需要时再替换，协议与地址不变） |
| D3 | 网关与控制器用 `--network host` + 仅 `127.0.0.1` 监听；`-R` 监听器默认只在 MegaCube loopback，容器因此可达 | `ssh -R` 无 `GatewayPorts` 时只绑定 loopback；打开 `GatewayPorts` 会把内部面暴露到 LAN。被否决：`GatewayPorts clientspecified`、把服务发布到 `0.0.0.0` |
| D4 | 应用面配置**单一来源**：`deploy/compose.env` + `deploy/private/*` 经脚本渲染成 k8s Secret/ConfigMap，不手抄 60 余个 `SPEECHNEXUS_*` | 手抄必然漂移；`deploy/compose.yaml` 已是这些键的权威清单 |
| D5 | 隧道端口避开 MegaCube 已占用端口：PG `15440`、S3 `19100`、网关 `18010`、控制器 `18020`、引擎 `18190` | 实测 `9000`、`15432`、`18100` 已占用；端口表进 `versions.env` 与运维手册 |
| D6 | 首个能力选**文件 ASR（Qwen3-ASR-1.7B）**，同步 TTS 作为第二条 | 文件 ASR 的 Job → 网关 → 引擎 → 制品链路最长，一次覆盖 PG、S3、网关、引擎四段；TTS 另需声音发布与资格链，会混入与本拓扑无关的变量 |
| D7 | 固定版本：仓库 SHA、镜像 digest、模型目录摘要、k8s 与 Docker 版本写入 `deploy/cluster/versions.env` | 两端都是 `arm64`，禁止混入 amd64 镜像；跨主机排障必须先排除版本漂移 |

## 5. 地址、端口与身份约定

| 项 | 取值 | 说明 |
| --- | --- | --- |
| k8s namespace | `speechnexus` | 全部应用面对象 |
| `node_id`（算力面） | `node-megacube-1` | 与 `deployment-units.md` 的「真实 HostNode」一致 |
| `gateway_id` | `gateway-megacube-1` | 网关入口所有权 |
| `instance_id` / `generation` | `instance-asr-1` / `1` | 重建实例时递增 generation |
| `pool_id` | `asr-batch` | 文件 ASR 逻辑池 |
| `workspace` | `workspace-default` | 首轮单工作区 |
| 网关控制 API | MegaCube `127.0.0.1:8010` ↔ 桥接 `:18010` | 仅组件凭据 |
| 控制器 API | MegaCube `127.0.0.1:8022` ↔ 桥接 `:18020` | 仅组件凭据 |
| PG 反向端口 | MegaCube `127.0.0.1:15440` → `postgres.speechnexus.svc:5432` | 隧道 `-R` |
| S3 反向端口 | MegaCube `127.0.0.1:19100` → `minio.speechnexus.svc:9000` | 隧道 `-R` |
| 引擎 | MegaCube `127.0.0.1:18190` | 由模板或手工 `docker run` 发布 |

## 6. 分阶段交付

每阶段以「可独立验收」为界；阶段内的脚手架与文档并入该阶段，不单列。每阶段的证据落在 `docs/archive/audits/evidence/<日期>-minimal-cluster/`：只保存命令输出、脱敏 JSON 与摘要，不保存音频、凭据或完整引擎日志。

### S0 基线与制品固定

**交付物**：`deploy/cluster/README.md`（拓扑、端口、身份、回退）、`deploy/cluster/versions.env`（SHA、镜像 digest、模型摘要、运行时版本）。

- 记录双端版本：`git rev-parse HEAD`、`kubectl version`、`docker version`、`ssh megacube 'docker version; uname -sr'`。
- 构建并固定共用镜像：`docker buildx build --platform linux/arm64 -f deploy/images/python.Dockerfile -t speechnexus-python:<sha> .`；同一镜像供 API、派发与 MegaCube 网关使用，MegaCube 侧以 `docker save | gzip | ssh megacube 'docker load'` 传输并核对 digest。镜像不可见时（`ErrImagePull`）用本机 `registry:2` + `host.docker.internal:5000` 兜底（Pod 内已确认该主机名可解析）。
- 记录镜像 digest：`speechnexus-python`、`node-controller`、ASR 引擎镜像；确认 `docker image inspect --format '{{.Architecture}}'` 均为 `arm64`。
- 记录模型目录摘要与大小（`sha256sum` 主权重、`du -sh`）。
- 定义资源窗口前置：MegaCube `MemAvailable ≥ 12 GiB` 且 35B LLM 不在运行。

**验收**：`versions.env` 中每一项都有命令输出可复现；`grep -c` 与清单条数一致。

**回退**：只读，无副作用。

### S1 应用面落地（PG、对象存储、迁移、API）

**交付物**：`deploy/cluster/app-plane/` 下 `namespace`、`postgres`、`minio`、`minio-init`、`migrate-job`、`api` 清单；`render-config.py`（`compose.env` + `private/` → Secret/ConfigMap）。

- `kubectl apply -f app-plane/namespace.yaml`；PG 与 MinIO 用 StatefulSet + `volumeClaimTemplates`（各 20 GiB），Service 暴露 `5432` / `9000`。
- `minio-init` 一次性 Job：复用 `deploy/init-object-storage.py` 创建并开启输入 / Artifact / 暂存三个 bucket 的版本化。
- 渲染配置：`POSTGRES_PASSWORD`、`MINIO_ROOT_*`、`SPEECHNEXUS_API_DATABASE_URL`、对象存储键、`SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL` 等进 Secret；`private/*.json` 进 Secret。`silero_vad.onnx` 不在应用面：VAD 只在网关侧使用，文件留在 MegaCube。
- 迁移 Job：`speechnexus-job-migrate --schema speech`，DSN 用迁移身份（非 owner 运行身份）。
- 角色与授权：复用[最小 Compose 手册 §6](../operations/compose-minimal.md#6-创建非-owner-运行角色与最小授权) 的 SQL，经 `kubectl port-forward svc/postgres 5432` 在 k8s PG 上执行；至少建立 `sn_api`、`sn_gateway`、`sn_gateway_query`、`sn_job_dispatch` 及 Job 准入读取身份。
- API Deployment：镜像 `speechnexus-python`（同 SHA 构建），`SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL=http://bridge.speechnexus.svc:18010`，`SPEECHNEXUS_ASSET_INSPECTOR=text`。

**验收**：

```bash
kubectl -n speechnexus get pods                 # 全部 Running / Job Completed
kubectl -n speechnexus logs job/minio-init      # 桶创建与版本化成功
kubectl -n speechnexus port-forward svc/api 8000:8000 &
curl -fsS localhost:8000/readyz | jq -e '.status=="ready"'
curl -fsS localhost:8000/v1/nexus/version | jq -e '.contract_digest'
```

**证据**：`kubectl get all`、`/readyz` 与 `/v1/nexus/version` 原样响应、迁移 head、受限身份各执行一次自己的查询。

**回退**：`kubectl delete namespace speechnexus`；不影响 MegaCube。

### S2 桥接（双向连通）

**交付物**：`deploy/cluster/bridge/`（Deployment、Service、`known_hosts`、SSH 密钥 Secret 的创建脚本，密钥不落仓库）。

- 生成专用密钥对（`ssh-keygen -t ed25519 -f .runtime/bridge_ed25519 -N ''`），公钥写入 MegaCube `~/.ssh/authorized_keys`，行首加 `restrict,no-pty,no-agent-forwarding,no-X11-forwarding`。
- 固定 MegaCube 主机指纹到 `known_hosts`（用公网入口 `[47.103.22.160]:6000` 的实际指纹），桥接以 `-o StrictHostKeyChecking=yes` 连接。
- 桥接命令：`ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -L 0.0.0.0:18010:127.0.0.1:8010 -L 0.0.0.0:18020:127.0.0.1:8022 -R 127.0.0.1:15440:postgres.speechnexus.svc:5432 -R 127.0.0.1:19100:minio.speechnexus.svc:9000 megacube`。
- 镜像：`deploy/cluster/bridge/Dockerfile`，基于 `debian:bookworm-slim` 装 `openssh-client`；先验证基础镜像内 `ssh -V` 可用再决定是否自建。

**验收**（`-R` 方向先可验）：

```bash
ssh megacube 'PGPASSWORD=... psql -h 127.0.0.1 -p 15440 -U sn_api -d speechnexus -c "select 1"'
ssh megacube 'curl -fsS http://127.0.0.1:19100/minio/health/live'
kubectl -n speechnexus delete pod -l app=bridge   # 重建后两条反向端口仍可用
```

**证据**：隧道建立日志（不含密钥）、两侧端口连通、重建后恢复时间。

**回退**：删除 Deployment/Service 与专用 authorized_keys 行；MegaCube 无残留。

### S3 算力面装配（网关 + 控制器）

**交付物**：`deploy/cluster/compute-plane/compose.yaml`（`network_mode: host`、`read_only`、`cap_drop: ALL`、网关与控制器两个服务）、`gateway.env`、`controller.env`、`approved-templates.json` 骨架。

- 网关：`SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL=postgresql://sn_gateway:...@127.0.0.1:15440/speechnexus`、`CONTROL_QUERY_DATABASE_URL`（`sn_gateway_query`）、`COMPONENT_TOKEN`、`INGRESS_LOCK_DIRECTORY=/var/lib/speechnexus/gateway-locks`、`NODE_ID`/`GATEWAY_ID` 显式、`FILE_ASR_CONFIG_FILE` 指向挂载的私有 JSON。
- 控制器：`COMPONENT_TOKEN`、`APPROVED_TEMPLATES_FILE`、`INTENT_STORE_PATH`、`DOCKER_SOCKET_PATH`、显式 `NODE_ID`、`ENVIRONMENT=production`。
- 私有 JSON：`gateway-file-asr.json` 的 `objects.endpoint` 用 `http://127.0.0.1:19100`，桶名与 API 一致；`gateway-file-asr-endpoint.json` 的 `address` 用 `127.0.0.1`、`port` `18190`（引擎起来后填最终值）。

**验收**：

```bash
ssh megacube 'curl -fsS http://127.0.0.1:8010/healthz; curl -fsS http://127.0.0.1:8010/readyz'
ssh megacube 'curl -fsS http://127.0.0.1:8022/healthz; curl -fsS http://127.0.0.1:8022/readyz'
kubectl -n speechnexus exec deploy/api -- curl -fsS http://bridge.speechnexus.svc:18010/healthz
kubectl -n speechnexus exec deploy/api -- curl -fsS http://bridge.speechnexus.svc:18020/healthz
```

**证据**：两端 `/readyz` 明细（PG、S3、Docker daemon、SQLite 各一项）、跨主机调用原样响应。

**回退**：`docker compose down`；删除本次新增容器，不动 QwenPaw 栈与既有 dev 单元。

### S4 引擎实例与注册

**交付物**：ASR 引擎运行记录、`register-engine` 或控制器 `prepare` 的回执、`gateway-file-asr-endpoint.json` 最终值。

- 首选控制器路径：模板声明镜像 digest、argv、只读模型挂载、内存上限、`127.0.0.1:18190` 端口、池 `asr-batch`，经 `runtime_intent` 触发 prepare，观察 `READY`。
- 备用路径（控制器模板不可用时）：手工 `docker run` 启动同一镜像与参数，再用 `speechnexus-register-engine` 登记；该命令读写 PG，从应用面以 k8s Job（API 同镜像、同 DSN 约定）执行，不在 MegaCube 上直连数据库。此路径在证据里明确标注「无控制器生命周期」，不得计作同构完成。
- 启动参数沿用既有资格值（`--gpu-memory-utilization`、`--max-num-seqs`、`--max-model-len`、`--enforce-eager`），不得为跑通而放宽。

**验收**：引擎 `/health` 200 且 `/v1/models` 返回预期 ID；实例行 `ready`、`generation=1`、`host_node_id=node-megacube-1`；控制器 SQLite 有对应回执；GPU/内存占用有前后读数。

**回退**：停止实例并确认端口关闭、`EngineCore` 进程消失、内存回落；保留停止证据。

### S5 公开文件 ASR 端到端

**交付物**：`deploy/cluster/app-plane/dispatch.yaml`、binding 发布与配方记录、一次真实 Job 的完整证据包。

- 发布 binding revision 与 `transcription-recipe.json`（`compose-minimal.md` §6/§7 的既定步骤），建立 `workspace-default` 配额；配方里的 `binding_revision_id`/`manifest_id` 必须指向本次已发布 revision，渲染后滚动重启 API 与派发。
- 派发 Deployment：`SPEECHNEXUS_JOB_DISPATCH_KINDS=asr`、`SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL=http://bridge.speechnexus.svc:18010`、镜像与 API 同 SHA。
- 用合成音频（正弦 + 语音样本各一）经公开 API 建 Job，轮询至终态，下载原始转写制品。

**验收**：

```bash
curl -fsS -X POST localhost:8000/v1/nexus/jobs -H 'content-type: application/json' -d @job.json
curl -fsS localhost:8000/v1/nexus/jobs/<id> | jq -e '.state=="succeeded"'
curl -fsS -o out.json localhost:8000/v1/nexus/artifacts/<id>/content && sha256sum out.json
```

同时核对：`attempt`/`lease` 各一组且 `released`、`cleanup=complete`、host 十二维与 quota 归零、`unknown_quarantined` 为 0、日志无正文与音频。

**证据**：Job 事件流末帧、制品字节数与 `sha256`、结算快照、网关 GET 回执。

**回退**：取消 Job → 确认停止与释放；不删除结算与 fencing 证据。

### S6 控制器生命周期（补齐同构）

**交付物**：`approved-templates.json` 定稿、prepare → READY → stop 三段的控制器回执。

- 把 S4 的引擎实例改为完全由控制器创建与停止；重复 `prepare` 只关联同一实例，迟到 `stop` 不命中替代实例。
- 停止后核对：执行层（容器退出码、端口关闭、无 `EngineCore`）与资源层（内存回落）双证据，且预算按证据释放。

**验收**：控制器 `/readyz` 通过、实例 generation 递增可见、停止前后 GPU/内存读数与预算行一致。

**回退**：回到 S4 手工路径并明确降级原因。

### S7 故障演练

| 场景 | 注入 | 期望 | 不接受 |
| --- | --- | --- | --- |
| 桥接中断 | 删除桥接 Pod / 断开 SSH | 新 Pod 重建，双向端口恢复；在途请求按传输失败处理，不判死 | 把断连当作任务失败或算力释放 |
| 网关重启 | `docker restart` 网关 | 原身份恢复，不重复派发、不重复发布 | 新建执行替换原身份 |
| 执行中引擎被杀 | 停止引擎容器 | 执行进入 unknown/quarantine 并保留预算，直到取得停止证据 | 直接标失败并释放预算 |
| PG 短暂不可用 | 缩容/重启 PG | API `/readyz` 反映依赖失败，恢复后继续；不产生伪成功 | 以进程存活冒充就绪 |
| API 重启 | 删除 API Pod | Job 事件可续读，已提交 Job 不丢 | 客户端被迫重建 Job |

每场景记录：注入命令、观测点、终态、恢复耗时、有无重复制品、预算是否提前释放。

### S8 固化、文档与拆除

- 新增运维手册 `docs/operations/minimal-isomorphic-cluster.md`：拓扑、端口表、启停顺序、排障入口、已知单点。
- 更新[部署单元规划](../architecture/deployment-units.md)§5 的「当前装配与证据」：新增一行本拓扑的装配范围与缺口（不改写 G5 结论）。
- 在[交付跟踪矩阵](delivery-tracking.md)的 O-PHYSICAL 行登记本次物理分机证据与仍缺项（备用 GPU、HA、实时链）。
- `deploy/cluster/teardown.sh`：按依赖逆序删除 k8s namespace、桥接密钥与 authorized_keys 行、算力面容器，并复查 MegaCube 容器清单与 S0 记录的基线逐项一致。

**验收**：拆除后 `kubectl -n speechnexus get all` 为空、MegaCube 无新增容器与监听端口、QwenPaw 单元状态不变。

## 7. 资源预算

| 组件 | requests | limits | 说明 |
| --- | --- | --- | --- |
| PostgreSQL | 512 Mi / 500m | 2 Gi / 2 | 单副本，数据 20 GiB |
| MinIO | 256 Mi / 100m | 1 Gi / 1 | 数据 20 GiB |
| Speech API | 512 Mi / 500m | 1.5 Gi / 2 | 解码在引擎侧 |
| Job 派发 | 256 Mi / 100m | 512 Mi / 1 | 单副本 |
| 桥接 | 64 Mi / 50m | 128 Mi / 200m | 单连接 |
| 引擎 | — | 内存上限按模板 | 只在 MegaCube，需要 GPU |

k8s 侧总量约 5 GiB，留出余量给 Docker Desktop 自身；MegaCube 侧前置检查 `MemAvailable ≥ 12 GiB`。

## 8. 安全与内容边界

- **受控例外**：本拓扑的业务内容经公网 SSH 隧道传输。隧道提供加密与完整性，但不等于「部署内网」；因此本阶段**只允许合成或已脱敏语料**，且必须在证据中写明该限制。生产替换为私有链路（WireGuard / VPC）时协议与地址不变，只换传输。
- **无新增公网监听**：MegaCube 不开放 8010/8022/18190；Caddy / Nginx / QwenPaw 端口不改。
- **密钥**：桥接用独立 ed25519 密钥，仅存 k8s Secret；authorized_keys 行限制为 `no-pty,no-agent-forwarding,no-X11-forwarding,no-user-rc`（`restrict` 会禁用端口转发，不能用于隧道）。凭据不写命令参数、日志或仓库。
- **最小权限**：应用面各进程用非 owner 角色；控制器独占 Docker socket；网关不持有容器管理权限。
- **不触碰生产服务**：不改 `qwenpaw-*`、`cube-ccba-*` 容器与单元；不动既有 `speechnexus-postgres` / `speechnexus-minio` 单机单元。
- **观测**：日志不落正文、音频、对象私有路径与凭据。

## 9. 风险登记

| 风险 | 影响 | 处置 |
| --- | --- | --- |
| 桥接是单点 | 两主机全链中断 | 单副本 + `ServerAlive` + 就绪探针 + 重建演练；明确登记为验证形态单点，不是 HA |
| Mac 休眠 / Docker Desktop 重启 | k8s 与隧道一起停 | 只在唤醒窗口内验收；证据记录窗口起止；不作为持续可用性结论 |
| MegaCube 35B LLM 自启 | 挤占 47 GiB，引擎起不来 | 启动前查 `MemAvailable` 与容器状态；不足则等待窗口，不停止他人服务 |
| 配置双源漂移 | 应用面行为与 Compose 不一致 | D4 单一来源渲染；`versions.env` 固定输入 |
| 端口被占 | 隧道或引擎绑定失败 | 端口表前置探测，冲突时改 `versions.env` 而非现场临时改 |
| 角色授权遗漏 | 运行期 `permission denied` | 每个角色用自身身份跑一次自己的查询；不借用 owner |
| 时钟偏差 | 租约/时间戳判读错误 | 两端 NTP 状态与 offset 记录进证据 |
| 镜像架构混用 | 引擎无法启动 | 只允许 `arm64`；`inspect` 核对后才部署 |

## 10. 已裁决（2026-11-20）

1. **内容边界**：接受「SSH 隧道 + 仅合成 / 脱敏语料」作为本阶段受控例外；替换为私有链路时只换传输，协议、地址与身份不变。
2. **首个能力**：文件 ASR（Qwen3-ASR-1.7B）；同步 TTS 作为第二条链路，不在本阶段验收。
3. **实施 issue**：按 Epic #342 拆四张实施卡——应用面 k8s 装配、跨主机桥接、算力面装配、端到端与故障演练，并关联交付矩阵的 O-PHYSICAL 行。

## 11. 完成定义

- S0–S8 全部有实测证据，且每条证据绑定版本、拓扑与时间。
- 一次合成音频的公开文件 ASR Job 在两台物理主机间完成，制品可下载、摘要一致、预算按证据释放。
- 五类故障各演练一次，结果与 §6 S7 的期望一致。
- 拆除脚本可把环境恢复到基线，MegaCube 生产服务零改动。
- 文档更新完成，缺口（备用 GPU、HA、实时链、生产 TLS、内容边界）显式登记，未关闭 G5。

[交付跟踪矩阵](delivery-tracking.md) · [部署单元规划](../architecture/deployment-units.md) · [最小 Compose 手册](../operations/compose-minimal.md)