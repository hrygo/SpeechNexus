---
title: "SpeechNexus 开发与测试环境"
status: active
date: 2026-10-22
---

# SpeechNexus 开发与测试环境

本文记录项目 Linux ARM64 参考开发与测试环境及其操作方式，不代表当前会话所在主机或服务的实时状态。执行前核验宿主、工具链、端口和项目隔离边界；当前交付范围见[跟踪矩阵](plans/delivery-tracking.md)，[原实施计划](plans/2026-10-22-speechnexus-implementation-plan.md)保留历史阶段。开发服务不是生产部署，也不构成容量、质量或恢复资格。

## 运行组件

| 组件 | 参考版本 / 地址 | 用途 |
| --- | --- | --- |
| Python | 3.14.9，由 `uv` 管理 | FastAPI、领域 / 契约测试与 fake |
| Node.js | 26.12.1，`pnpm` 12.14.0 | React / TypeScript 工作台 |
| 无头浏览器 | Playwright 1.64.1 + Chromium 156 | 浏览器采集、Web Audio、MediaRecorder 与 WebSocket 冒烟验证 |
| 媒体工具 | PyAV 19、imageio-ffmpeg 0.6、soundfile 0.14 | 24 kHz PCM16、16 kHz ASR 重采样、WAV、MP3 编解码与测试音频生成 |
| PostgreSQL | 16.15，`127.0.0.1:55432` | 任务、幂等、outbox、配额与事务测试 |
| MySQL / InnoDB | 8.4.8，`127.0.0.1:55434` | 双库协调事务验证；不构成产品 MySQL 支持 |
| MinIO | 2025-09-07，`127.0.0.1:59000` | 私有 staging 与 Artifact 集成测试 |
| RTK | 1.0.1 | Codex 命令输出压缩 |
| codebase-memory-mcp | 0.12.1 | 本地结构查询与索引覆盖检查 |

Node 版本以 `.nvmrc` 的 `26.12.1` 为准；切换本机 shell 后，`make dev-status` 会校验 Node 与 pnpm 的精确版本。

`make dev-status` 中的 PostgreSQL 版本项只检查二进制的 `--version` 命令能否运行，并不比较输出或核实正在监听的服务版本；监听检查也只排除指定端口上的通配地址，不能证明所有监听地址均为回环。需要版本和网络边界证据时应在目标宿主另行核验。

PostgreSQL 与 MinIO 使用用户级 systemd 服务，状态和对象数据保存在被 Git 忽略的 `.runtime/`。服务只绑定回环地址；首次初始化的 PostgreSQL 对本地 socket 与回环连接使用 `trust`，本机其他用户也可能访问，不能将回环地址当作身份隔离。已有数据目录的配置不会由脚本重新初始化，实际权限须核验。`DATABASE_URL_DEV`、`DATABASE_URL_TEST`、`S3_ENDPOINT` 等非秘密地址位于 `.runtime/config/dev.env`，MinIO 凭据位于权限为 `0600` 的 `.runtime/config/minio.env`。

协调测试用的 MySQL 是钉住摘要 `mysql@sha256:2952e3be…` 的项目受管容器，由 `scripts/dev-env/mysql-service.sh` 幂等安装与启动，数据保存在 `.runtime/data/mysql`（初始化后归容器内 `mysql` 账户），根口令写在权限为 `0600` 的 `.runtime/config/mysql.env`。参考环境不使用用户 systemd 单元管理该容器，而由 docker 的 `unless-stopped` 重启策略在守护进程启动时自动拉起；`make dev-up`、`make dev-bootstrap` 与 `make verify` 会在需要时调用同一脚本。实际 socket 权限和既有容器状态需在目标主机核实。

执行这些入口前，还须检查名为 `speechnexus-coordination-mysql` 的已有容器：脚本在标签或镜像不匹配时会强制移除该容器后重新创建，不会因其不属于本项目而自动退出。若归属或数据用途不明，应先停止操作并核实，不能依靠脚本判断移除是否获授权。`make dev-down` 也会停止同名容器。

PostgreSQL 与 MinIO 分别限制为最多 4 GiB 内存、2 个 CPU 核和 512 个任务，MySQL 容器限制为 2 GiB 内存和 2 个 CPU 核，避免开发负载无界挤占同一主机上的共享服务。该限制是开发环境的保护措施，不是生产容量声明。

## 初始化与日常检查

```bash
make dev-bootstrap
make dev-status
make dev-down
make dev-up
make verify
```

`make dev-bootstrap` 会准备 Python / Node 工具链和 Chromium，校验脚本指定的发行包摘要，初始化本地数据目录，建立 `speechnexus_dev` / `speechnexus_test` 数据库和 `speechnexus-dev` / `speechnexus-test` bucket，安装并启动隔离的协调 MySQL 容器，然后运行健康检查。它执行 `uv sync` 而未指定 `--locked`；pnpm 有锁文件时使用 `--frozen-lockfile`，缺少锁文件时使用普通安装。重复执行会复用已有数据与浏览器缓存，但会停止并重新启动上述用户级服务。

该脚本还会用符号链接替换 `~/.local/bin` 下的 `uv`、`uvx`、`node`、`npm`、`npx` 和 `ffmpeg` 同名入口，并在未启用时为当前用户启用 systemd linger。执行前须核对这些用户级入口及服务的归属与用途；这不只是仓库内的依赖安装。

Python 开发依赖由根目录 `pyproject.toml` 和 `uv.lock` 锁定，后端与执行网关是 workspace 成员。首次初始化后使用 `uv sync --locked --all-packages --all-groups`；执行工具时使用 `uv run pytest`、`uv run ruff check` 等命令。

当前 shell 需要数据库与对象存储地址时运行：

```bash
source scripts/dev-env/activate.sh
```

该文件只导出 `dev.env` 中的开发地址和桶名，不设置下述 `SPEECHNEXUS_TEST_*` 测试变量或导出 MinIO 凭据；按测试入口另行配置隔离服务。

`make verify` 包含参考 Linux 宿主的服务检查、Python 静态与测试、契约与媒体检查、Web 检查、构建和部署配置检查；实际入口及运行态副作用以 `scripts/dev-env/verify.sh` 和[验证指南](agent-verification.md)为准。它检查 systemd MinIO 后仍尝试确保默认测试 MinIO 容器，二者默认争用 `127.0.0.1:59000`；启动前须明确安排隔离端点和服务，不能假定默认组合可同时运行。可分别使用 `make contracts-check`、`make media-smoke` 和 `make browser-smoke` 调试对应部分。

`make contracts-check` 校验 `contracts/` 中按名称匹配的 OpenAPI 和 JSON Schema，并显式验证其中相对文件及片段 `$ref` 指针；该指针检查不覆盖其他文件、外部 URL 或 `file://` 引用。`CONTRACT_BASE_REF=origin/main make contracts-compat` 检查 OpenAI 公共契约的明显破坏性变更；使用前核验基线。`make integration-test` 与 `make coordination-test` 要求同时设置隔离的 `SPEECHNEXUS_TEST_DATABASE_URL` 和 `SPEECHNEXUS_TEST_MYSQL_URL`，缺任一变量直接失败，并在 DSN 指向本机默认端点时先调用[协调 MySQL 脚本](../scripts/dev-env/mysql-service.sh)启动实例。`make verify` 在环境检查前确保隔离 MySQL 容器，并为缺失配置提供默认隔离 DSN；直接调用相关共享事务 pytest 时，两项均缺会跳过，只配置一项会失败。`make integration-test` 还要求隔离 MinIO 端点与测试凭据；资产测试使用版本化独立桶，缺少配置失败。默认本机实例可由 `scripts/dev-env/minio-test-service.sh` 启动，不能与占用同一端口的 systemd / Compose MinIO 同时启动。配置与证据范围见[资产发布存储单元](components/production-assets.md)。结构检查与契约测试不替代业务路由、事件序列或真实模型 API 测试。新增契约和类型生成的门禁见[整体技术方案](architecture/technical-blueprint.md#3-契约与类型生成)。

浏览器冒烟使用合成音频设备和自动授予的麦克风权限，检查 MediaRecorder 非空输出、离线 Web Audio 渲染，以及本地 WebSocket 文本 / 二进制回环；它不验证产品播放缓冲或真实权限交互。真实麦克风、系统音频采集和跨设备行为仍需在有实体设备的获准环境中验收。

## 隔离边界

宿主可能存在共享 PostgreSQL、Redis、MinIO、vLLM 或其他用户的服务。SpeechNexus 的测试不得写入共享实例或复用其容量；上述独立端口和 `.runtime/` 是参考开发边界，使用前仍需核实。已有 vLLM 服务只可用于明确授权的 adapter 探针，不能作为生产绑定或容量证明。

真实模型测试需先记录模型、引擎、驱动、精度与 manifest；缺少这些证据的组合保持 experimental。默认确定性测试使用 fake 和合成数据，不加载语音模型或使用私人音频；真实实验须有单独授权和证据。

## 重启与恢复

参考部署使用用户级 systemd 管理 PostgreSQL 与对象存储，并可由 docker 重启策略拉起协调 MySQL 容器。systemd、linger、容器与服务是否启用须在目标主机实测；确认环境适用后可用 `make dev-up` 启动项目服务。数据库和对象数据不会因正常停止服务而删除。

恢复到新机器或新用户时运行 `make dev-bootstrap`，不要复制 `.runtime/config/minio.env` 的凭据到 Git。破坏性重置需由维护者单独确认数据范围。
