---
title: "SpeechNexus 开发与测试环境"
status: active
date: 2026-10-22
---

# SpeechNexus 开发与测试环境

本机是 Linux ARM64 开发与测试主机，用于先完成 P0 机器契约、fake 与事务验证，再按[实施计划](plans/2026-10-22-speechnexus-implementation-plan.md)推进 P1。这里的开发服务不是生产部署，也不构成容量、质量或恢复资格。

## 运行组件

| 组件 | 本机版本 / 地址 | 用途 |
| --- | --- | --- |
| Python | 3.12，由 `uv` 管理 | FastAPI、领域 / 契约测试与 fake |
| Node.js | 22，`pnpm` 10 | React / TypeScript 工作台 |
| 无头浏览器 | Playwright 1.64.1 + Chromium 156 | 浏览器采集、Web Audio、MediaRecorder 与 WebSocket 冒烟验证 |
| 媒体工具 | PyAV 19、imageio-ffmpeg 0.6、soundfile 0.14 | 24 kHz PCM16、16 kHz ASR 重采样、WAV、MP3 编解码与测试音频生成 |
| PostgreSQL | 16.15，`127.0.0.1:55432` | 任务、幂等、outbox、配额与事务测试 |
| MinIO | 2025-09-07，`127.0.0.1:59000` | 私有 staging 与 Artifact 集成测试 |
| RTK | 1.0.1 | Codex 命令输出压缩 |
| codebase-memory-mcp | 0.12.1 | 本地结构查询与索引覆盖检查 |

PostgreSQL 与 MinIO 使用用户级 systemd 服务，状态和对象数据保存在被 Git 忽略的 `.runtime/`。服务只绑定回环地址；PostgreSQL 仅允许本机连接。`DATABASE_URL_DEV`、`DATABASE_URL_TEST`、`S3_ENDPOINT` 等非秘密地址位于 `.runtime/config/dev.env`，MinIO 凭据位于权限为 `0600` 的 `.runtime/config/minio.env`。

两个服务分别限制为最多 4 GiB 内存、2 个 CPU 核和 512 个任务，避免开发负载无界挤占同一主机上的共享服务。该限制是开发环境的保护措施，不是生产容量声明。

## 初始化与日常检查

```bash
make dev-bootstrap
make dev-status
make dev-down
make dev-up
make verify
```

`make dev-bootstrap` 会安装锁定的 Python / Node 工具链和 Chromium，校验发行包摘要，初始化本地数据目录，建立 `speechnexus_dev` / `speechnexus_test` 数据库和 `speechnexus-dev` / `speechnexus-test` bucket，然后运行健康检查。重复执行会复用已有数据与浏览器缓存。

Python 开发依赖由根目录 `pyproject.toml` 和 `uv.lock` 锁定，后端与执行网关是 workspace 成员。首次初始化后使用 `uv sync --all-packages --all-groups`；执行工具时使用 `uv run pytest`、`uv run ruff check` 等命令。

当前 shell 需要数据库与对象存储地址时运行：

```bash
source scripts/dev-env/activate.sh
```

`make verify` 运行服务健康检查、Shell / JavaScript 语法检查、Ruff、mypy、契约检查、PCM / WAV / MP3 解码检查、Chromium 虚拟麦克风与 Web Audio 冒烟检查、WebSocket 文本和二进制往返，并在已有 Python 测试时执行 pytest。可分别使用 `make contracts-check`、`make media-smoke` 和 `make browser-smoke` 调试对应部分。

P0 建立 `contracts/` 后，`make contracts-check` 会校验 OpenAPI 文档和 JSON Schema 的规范结构。当前没有契约文件时明确显示跳过；该检查不替代语义负例、状态机或真实 API 测试。

无头 Chromium 使用合成音频设备，适合自动验证采集权限、MediaRecorder 与播放缓冲路径；本机未发现采集声卡，真实麦克风、系统音频采集和跨设备行为仍需在有实体设备的获准环境中验收。

## 隔离边界

本机另有共享 PostgreSQL、Redis、MinIO、vLLM 与其他用户的服务。SpeechNexus 的测试不得写入共享实例或复用其容量；上述独立端口和 `.runtime/` 是默认开发边界。现有 vLLM 服务只可用于明确标记的 adapter 探针，不能作为生产绑定或容量证明。

真实模型测试需先记录模型、引擎、驱动、精度与 manifest；缺少这些证据的组合保持 experimental。当前开发闭环先依赖 fake，不加载语音模型，也不使用私人音频。

## 重启与恢复

用户级 systemd 服务已启用，并为本账号开启 linger；注销后服务继续运行。主机启动后由用户 systemd 拉起数据库与对象存储。服务未运行时执行 `make dev-up`。数据库和对象数据不会因服务停止而删除。

恢复到新机器或新用户时运行 `make dev-bootstrap`，不要复制 `.runtime/config/minio.env` 的凭据到 Git。破坏性重置需由维护者单独确认数据范围。
