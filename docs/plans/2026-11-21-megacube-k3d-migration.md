# MegaCube 上部署 k3d：网络规划与迁移方案

目标：把开发环境收敛到一台 k8s 上，去掉跨机隧道与端口补齐器，同时验证统一内存设备上的
显存切分语义。前提是**不影响 QwenPaw、npm 私服与 SSH**。

## 一、现状

MegaCube（`47.103.22.160:6000`，aarch64，NVIDIA GB10，驱动 580.178.04，宿主 121 GB 内存、
92 GB 可用）。

| 容器 | 状态 | 归属 | 处置 |
| --- | --- | --- | --- |
| `qwenpaw-reranker`（127.0.0.1:8013） | 4 周 | QwenPaw | 保留；迁入集群后停掉重复的 systemd 单元 |
| `qwenpaw-embedding`（127.0.0.1:8012） | 4 周 | QwenPaw | 同上 |
| `zymail-registry-verdaccio-1`（127.0.0.1:4873） | 22 小时 | npm 私服 | 保留，不动 |
| `enterprise-agent-eval-poc-*`（6 个） | 2 个月 | 评估环境 | 保留运行；迁移手册见另文 |
| `speechnexus-*`（5 个运行 + 1 个一次性） | 3 小时 | 本项目算力面 | **停**，迁移后由集群接管 |
| `speechnexus-paperclip-minio` | 3 周 | 本项目 | **停** |
| `speechnexus-coordination-mysql` | 3 周 | 本项目 | **停** |
| `cube-ccba-qwen36-35b-nvfp4` | 已退出 17 小时 | QwenPaw 侧模型 | 迁入集群，**按需启动** |
| `cube-ccba-qwen36-27b-nvfp4` | 已退出 2 个月 | QwenPaw 侧模型 | 迁入集群，**不启动** |
| `caddy` / `nginx` / `frpc` / `mihomo` / `qdrant` / `qwenpaw-hub` / `qwenpaw-kb-*` / `dgx-dashboard*` | — | 宿主服务 | 全部不动 |

两处已定：

- `qwenpaw-embedding` 与 `qwenpaw-reranker` 同时存在容器与同名 systemd 单元。以容器为准，
  迁入集群后停用 systemd 单元，避免两套并存。
- 已退出的两个 `cube-ccba-qwen36-*` 不再以 Compose 方式拉起，改为集群内的工作负载定义。

## 二、网络规划

### 为什么不能等默认值

宿主自己的局域网是 `172.18.24.0/22`（`wlP9s9`，WiFi），默认路由指 `172.18.24.1`——**SSH 就走这条**。
而 k3d 与 Docker 默认从 `172.18.0.0/16` 起分配网段，直接压在局域网段上。装下去可能把到 172.18.x
的路由搅乱，连当前会话一起断。

现有 Docker 已占用 `172.17/19/20/21`；宿主无任何 `10.x` 路由。

### 定下来的值

| 项 | 值 | 理由 |
| --- | --- | --- |
| k3d Docker 网络 | `10.99.0.0/16` | 与宿主路由、现有 Docker 网段都不重叠 |
| API 监听 | `127.0.0.1:16550` | 只绑回环，不暴露；避开 6443 |
| traefik | 禁用 | 不需要入站，避免占用 80/443 |
| Pod 网段 / Service 网段 | 保持 k3s 默认 `10.42/10.43` | 在节点容器 netns 内，宿主不可见 |

### SSH 保障措施（硬约束）

1. 不修改默认路由，不动 `wlP9s9`，不写宿主 iptables。
2. 变更前快照 `ip -4 route`、`ip addr`、`docker network ls`、`ss -lntp`；变更后逐项比对，
   只允许新增 `10.99.0.1` 这条。
3. 改动期间保持一条已建立的 SSH 会话不关，另开一条执行变更。
4. 确认带外控制台（云厂商 VNC/串口）可用后再动手。
5. 创建集群的命令里带上失败即删：任一步失败立即 `k3d cluster delete`，不留下半成品。
6. 若连接中断：从带外控制台执行 `k3d cluster delete` 与 `docker network rm k3d-net`。

### 前置

`k3d` 与 `kubectl` 当前都未安装。安装到 `/usr/local/bin` 需要 sudo；若不便提权，装到用户目录
并把路径加进当前会话即可，不需要系统级改动。

## 二之二、可执行清单

每条命令都已在实机核对过前提（无 sudo、arm64、CDI 已配、GitHub 可达）。

### 1. 安装（不需要 sudo）

```sh
mkdir -p ~/.local/bin
TAG=$(curl -fsSL https://api.github.com/repos/k3d-io/k3d/releases/latest \
  | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4)
curl -fsSL -o ~/.local/bin/k3d \
  "https://github.com/k3d-io/k3d/releases/download/$TAG/k3d-linux-arm64"
chmod +x ~/.local/bin/k3d

KV=$(curl -fsSL https://dl.k8s.io/release/stable.txt)
curl -fsSL -o ~/.local/bin/kubectl \
  "https://dl.k8s.io/release/$KV/bin/linux/arm64/kubectl"
chmod +x ~/.local/bin/kubectl
```

不用 `curl … | bash` 的安装脚本：脚本会消费 stdin，把 SSH 会话一起吃掉（实测断连）。
远端执行时统一带 `ssh -n`，不把本地 stdin 接到远端命令上。

### 2. 网络

```sh
docker network create k3d-net --subnet 10.99.0.0/16
```

### 3. 集群

```sh
~/.local/bin/k3d cluster create gpu \
  --network k3d-net \
  --api-port 127.0.0.1:16550 \
  --gpus all \
  --servers 1 --agents 0 \
  --k3s-arg "--disable=traefik@server:0" \
  --wait
```

`--gpus all` 在本机可用：Docker 已通过 CDI 提供 GPU（`/var/run/cdi/nvidia.yaml`），
实测 `docker run --gpus all` 能在容器内看到 `NVIDIA GB10`。

### 4. 安全核对（每条都要看结果）

```sh
ip -4 route | diff /tmp/pre-route.txt -     # 只允许新增 10.99.0.1
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8012/v1/models
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8013/v1/models
```

两个 `curl` 都必须是 `200`；路由差异出现 `172.18` 就立即回滚。

### 5. 回滚

```sh
~/.local/bin/k3d cluster delete gpu
docker network rm k3d-net
```

## 三、阶段 0：统一内存上的显存切分探针

要回答的问题只有一个：**在 GB10 统一内存上，`CUDA_DEVICE_MEMORY_LIMIT` 是否真的约束得住**。
`nvidia-smi` 的显存字段全是 `[N/A]`，所以不能靠它判断。

探针分五步，每步都留读数：

| 步 | 动作 | 期望读数 |
| --- | --- | --- |
| 1 | Pod 内 `nvidia-smi` | 确认设备可见；显存字段可能是 `[N/A]` |
| 2 | `torch.cuda.get_device_properties(0).total_memory` | 未限制时应接近宿主可用内存 |
| 3 | 加 `LD_PRELOAD=libvgpu.so` 与 `CUDA_DEVICE_MEMORY_LIMIT=6g`，重复第 2 步 | 若返回 6 GiB，说明拦截生效 |
| 4 | 尝试分配 8 GiB | 应 `CUDA_ERROR_OUT_OF_MEMORY` |
| 5 | 尝试分配 4 GiB | 应成功 |

三种结论，对应三种后续：

- **第 3 步变成 6 GiB 且第 4 步失败**：切分可信，按分区模式设计，HAMi 进集群。
- **第 3 步仍报整机内存**：库在统一内存上没生效，HAMi 不可用；退回"约定式共享"——
  不装 device plugin，靠显存预算与实测监控，理由是余量本就充足（QwenPaw 4.7 GiB / 92 GiB 可用）。
- **libvgpu 在 arm64 上加载失败**：同样退回，并把原因写进差异清单。

探针本身不改变宿主配置，Pod 跑完即删；显存占用峰值 8 GiB 且持续数十秒。

## 三之二、探针结论（已实测）

在 MegaCube（NVIDIA GB10，统一内存，驱动 580.178.04，aarch64）上完成验证。用
`projecthami/hami:v2.8.2` 的 arm64 镜像取出 `libvgpu.so.v2.8.2`（ELF aarch64，679 KB），
以 `LD_PRELOAD` 注入普通容器，不依赖 Kubernetes。

| 组别 | 容器内可见显存 | 分配 8 GiB | 分配 4 GiB |
| --- | --- | --- | --- |
| 无限制 | 121.66 GiB | 成功 | 成功 |
| 只注入 libvgpu，不设限额 | 121.66 GiB | 成功 | 成功 |
| 注入 libvgpu + `CUDA_DEVICE_MEMORY_LIMIT=6g` | **6.00 GiB** | **OOM 失败** | 成功 |

结论：**统一内存上切分成立**。libvgpu 既能改小可见总量，也能在分配时拒绝超限，失败信息是
标准的 CUDA OOM，应用侧无需改动。OOM 报错里明确写着 `GPU 0 has a total capacity of 6.00 GiB`。

一个必须记住的副作用：容器内的 `nvidia-smi` 仍显示 `[N/A]`，**不能**用它判断限额是否生效；
判断标准是 `torch.cuda.get_device_properties(0).total_memory`。

由此确定参数：分区模式下 vLLM 的 `--gpu-memory-utilization` 取约 0.9（用满自己那份），
开发机整卡共享时取 0.125。两者是同一个开关的不同取值，不写进仓库。

## 四、阶段 1：迁移

前提是阶段 0 有明确结论、且 QwenPaw 在探针前后无异常。

1. 建集群（10.99.0.0/16、API 绑 127.0.0.1:16550、禁 traefik）。
2. 导入现有镜像（`speechnexus-python`、`speechnexus-node-controller` 等）到 k3d 的
   containerd，不重新构建。
3. 应用面清单原样 `kubectl apply`：它们是标准 k8s 对象，卷从 `hostPath/emptyDir` 换成
   `local-path`。
4. 算力面改用 `KubernetesRuntime`：引擎由控制器创建为 Pod，经 Service 暴露，
   **删除 `engine-shim`**。
5. 删除 bridge 相关全部内容（`tunnel.sh`、`setup.sh`、`deployment.yaml`、`Dockerfile`）。
6. 删除 `render-web-upstreams.py` 的 ClusterIP 绕法：集群内 Service DNS 正常，前端直接用
   `api.speechnexus.svc:8000`。

迁移完成后停掉旧的 5 个 Compose 容器。

## 五、回滚

| 层 | 动作 | 影响 |
| --- | --- | --- |
| 集群 | `k3d cluster delete` | 只删容器与网络，宿主无残留 |
| 网络 | `docker network rm k3d-net` | 删掉 10.99.0.0/16 那条路由 |
| 数据 | 保留 Compose 卷不删 | 老环境可原样拉起 |

回滚不改动 QwenPaw、npm 私服与宿主配置。

## 六、验收

| 断言 | 判红条件 |
| --- | --- |
| SSH 未受影响 | 变更期间连接中断，或 `ip -4 route` 出现非 10.99 的新增项 |
| QwenPaw 未受影响 | 探针前后 `/v1/models` 非 200，或显存占用抖动超过 100 MiB |
| 网络隔离 | Pod 网段出现在宿主路由表 |
| 切分语义 | 探针没给出三种结论之一，或结论与配置不符 |
| 迁移完整 | bridge 或 `engine-shim` 仍有引用 |