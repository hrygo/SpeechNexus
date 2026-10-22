---
title: "SpeechNexus 开发与测试环境就绪审查"
status: reviewed
date: 2026-10-22
---

# SpeechNexus 开发与测试环境就绪审查

本审查核对当前 Linux ARM64 主机是否具备启动 P0 契约、领域状态与 fake 验证的条件，并记录 P1 真实推理仍缺少的证据。结论只覆盖本机开发 / 测试准备；文档审查、服务启动或一次冒烟检查都不能证明语音质量、容量或生产资格。

## 1. 结论

P0 可以在本机启动：Python、Node、PostgreSQL 和 S3 兼容对象存储均可独立使用，契约工具链已安装，数据库事务与对象读写已通过冒烟检查。P1 的 fake 路径也已具备基础设施。

真实引擎路径目前只能作为受控探针。主机存在其他用户的共享 vLLM 服务，但没有合适的容量隔离、TTS 绑定、模型 revision 与资格报告；这些服务不能作为 SpeechNexus 的生产资源池。当前约 32 GiB 可用内存和共享推理负载也不足以推断实时并发或长时稳定性。

## 2. 已验证环境

核验日期为 2026-10-22，执行身份为普通用户 `huangzhonghui`。

| 项目 | 当前状态 | 证据 |
| --- | --- | --- |
| 主机 | Linux 7.0 aarch64，20 CPU，约 121 GiB 内存，3.2 TiB 可用磁盘 | `lscpu`、`free -h`、`df -h` |
| Python | 3.12.3，项目虚拟环境由 `uv` 管理 | `uv 0.13.5`，`uv sync --all-groups` |
| Web 工具 | Node 22.23.2，pnpm 10.34.7 | `node --version`、`pnpm --version` |
| 浏览器 | Playwright 1.64.1，Chromium 156 | 无头启动、虚拟麦克风录制、Web Audio 与 WebSocket 文本 / 二进制往返通过 |
| 媒体 | PyAV 19.0.2、imageio-ffmpeg 0.6、soundfile 0.14 | 24 kHz PCM16 生成、16 kHz ASR 重采样、WAV / MP3 编解码通过 |
| PostgreSQL | 16.15，仅监听 `127.0.0.1:55432` | 用户级 systemd 服务 active；查询与临时表事务成功 |
| 对象存储 | MinIO `RELEASE.2025-09-07T16-13-09Z`，仅监听 `127.0.0.1:59000` | 健康检查、bucket 列举、上传 / 读取 / 删除成功 |
| 契约工具 | jsonschema、OpenAPI validator、PyYAML、pytest、ruff、mypy | `scripts/dev-env/check.sh` 通过 |
| 数据隔离 | `speechnexus_dev`、`speechnexus_test` 与 `speechnexus-dev`、`speechnexus-test` | PostgreSQL 与 MinIO 独立实例 |

发行包下载使用固定版本；`uv` 与 MinIO 压缩包 / 二进制的 SHA-256 在引导时校验。PostgreSQL 使用 Ubuntu 已签名软件源，固定为 16.15-0ubuntu0.24.04.1。Python 依赖记录在 `uv.lock`。

## 3. 安全与运行边界

PostgreSQL 与 MinIO 都是用户级服务，数据、日志和配置位于被 Git 忽略的 `.runtime/`。对象存储凭据只存在于权限 `0600` 的本地环境文件。数据库仅允许回环地址使用 trust 认证，适合当前单机开发；不能把该认证方式带入共享或生产部署。

当前账号不在 Docker 组，Docker 套接字访问被拒绝。开发服务因此采用用户级二进制和 systemd，不依赖 Docker。现有共享容器、数据库、缓存与推理服务不属于 SpeechNexus 测试范围。

用户级 systemd 服务已启用，账号 linger 已开启；注销后服务继续运行，主机启动时由用户 systemd 拉起。运行状态仍需通过 `make dev-status` 验证。

## 4. 推理环境与剩余缺口

本机可观察到共享 vLLM 实例，包括 ASR 与 embedding / reranking 端口；快照时未确认可用的 TTS 或音色创作绑定。共享实例缺少 SpeechNexus 的 execution gateway、租约、fencing、取消确认和总池预算，不能直接承担平台调度或实时 SLO。

开始真实 adapter 资格验证前仍需：

1. 固定引擎、模型、tokenizer / codec、驱动、精度和部署拓扑，并记录摘要。
2. 验证输入格式、采样率、commit 顺序、request_id、status / abort 与停止证据。
3. 建立父物理预算和独占测试窗口，避免与其他用户负载混测。
4. 完成标准文件 ASR、文本 TTS、实时 ASR/TTS 与一条音色复用路径的分别验收。
5. 在已有 headless Chromium 与虚拟音频基础上，补齐真实麦克风、系统音频、网络中断和跨设备验收。

在这些证据出现前，真实绑定保持 experimental；F-01 至 F-03 涉及的并发 claim、取消、fencing、计量与发布边界继续由 fake 和 PostgreSQL 事务测试先覆盖。

## 5. 后续入口

P0 可从机器契约、负例和 fake 开始，开发环境不要求加载模型。具体操作见[开发与测试环境](../development-environment.md)。每次准备完成后运行 `make dev-status`；失败时应先恢复本地基础设施，再继续依赖数据库或对象存储的工作。
