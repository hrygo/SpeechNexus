---
title: "B03 默认文件 TTS 的真实 CustomVoice 持久执行探针"
status: reviewed
version: "0.1.0"
date: 2026-10-26
---

# B03 默认文件 TTS 的真实 CustomVoice 持久执行探针

本次验证从 A 已持久化的 TTS 命令开始，经默认 `file_tts_v1` 配置装配、原提交入口、
共享文件 worker、真实 vLLM-Omni CustomVoice、隔离 PostgreSQL 生成区间和隔离 MinIO
私有回执，最后重启网关并按原身份重放。它验证了默认工厂与真实引擎的组合；不包含
公开 Job/发布/SDK 下载、A 的结算、可信停止/释放或物理集群资格。

## 固定环境

| 项目 | 固定事实 |
| --- | --- |
| 镜像 | `vllm/vllm-omni:latest`，`sha256:5a12eac1a2dd3540a6ec29e42ef0dd5286c75ee07f68e497cb5f0df6e1311a52` |
| 引擎版本 | 镜像内 vLLM 0.32.0，`vllm-omni serve --omni` |
| 模型服务名 | `qwen3-tts-1p7b-custom` |
| 启动参数 | `--omni`、24 kHz、`--gpu-memory-utilization 0.06`、`--max-model-len 4096`、`VLLM_USE_V2_MODEL_RUNNER=0`、16 GiB 容器内存 |
| 拓扑 | 单 GB10 主机、一个 TTS 容器、一个隔离 bridge 网络；不是物理多节点资格 |
| 存储 | 隔离 PostgreSQL 55432 与隔离 MinIO 59000，测试 schema/bucket 用后删除 |
| 受保护服务 | `cube-ccba-qwen36-35b-nvfp4` 启动时间前后完全一致，未被停止或重启 |

镜像通过 64 位十六进制 bearer token 限制受控 endpoint；配置文件和对象凭据只存在于
本机忽略的 `.runtime/`。报告不记录模型路径、token、请求正文或音频内容。

## 首次失败和修复

第一次真实请求在 195 秒就绪后收到引擎 400：

```text
Invalid language 'Zh'. Supported: Auto, Beijing_Dialect, Chinese, English, ...
```

公开命令契约使用 BCP-47 语言标签（`zh`、`zh-CN`），vLLM-Omni 的 CustomVoice 接口使用
固定厂商名称（`Chinese`）。适配器原样透传，导致真实命令在引擎边界失败，音频回执未出现。
失败证据保留在忽略目录的 `20261026T041743Z-e19ffb34`，没有改写为成功。

修复位于 `VllmTtsSynthesizer` 的 vendor 边界：按主语言子标签转换已声明语言
（`zh`/`zh-CN`→`Chinese`、`en`/`en-US`→`English` 等），冻结策略摘要和执行身份不变。
回归覆盖英文、中文区域标签及未声明语言时不发送字段；定向 34 项通过。

## 成功证据

成功 run 为 `.runtime/probes/default-real-tts/20261026T043513Z-f7eb2aac`：

| 事实 | 结果 |
| --- | --- |
| TTS 就绪 | 196 秒 |
| 提交到音频回执 | 4.543 秒 |
| 引擎请求号 | `file-command-17ca7d4c446f40a4ab9bc0659a8babeb` |
| 回执格式 | `audio/wav`，24 kHz、mono、PCM16 |
| 对象 | 307244 字节，单版本 `5fa37693-6b7d-4ae5-8361-b5a1b8fd07b7` |
| 对象摘要 | `sha256:34ef59da9fc5511e593620bb42f744e2af65384db8b695bcdef2ae26ae6e0214` |
| 源 PCM | 153600 samples，6400 ms |
| 生成区间 | `[0, 153600)`，`generated-file-command-17ca7d4c446f40a4ab9bc0659a8babeb` |
| submit intent | `generated_pcm_samples=153600`，输出流 `operation-test.attempt-alt` |
| 重放 | 首次非重放；重启后原身份重放保持 `unknown_quarantined`，引擎请求号不变 |
| 预算 | 前后计数完全一致；无停止证据行 |

网关重启后仍能读取同一个对象版本与回执，且没有再次调用引擎。回执登记只证明私有 staging
结果；执行状态仍为 `executing`，由 A 的原事务负责后续结算、发布或停止处理。

### 三种交付格式

同一镜像和模型在 run `20261026T051302Z-bf1ec5c0` 中串行提交 WAV、MP3、PCM 三条命令：

| 交付 | 对象字节 | 对象摘要 | 提交到回执 | 解码/样本核对 |
| --- | --- | --- | --- | --- |
| WAV | 311084 | `sha256:e6c21a949f699106eb12a7b0ab0cefdabe0b79735ba03a93b20bc166e604fc0d` | 4.530 秒 | RIFF/WAVE，PCM 311040 字节 |
| MP3 | 52460 | `sha256:ecf0982d4a64d5f144ae0025ad2cc12d15d03712697532c9884b80f93e908bdf` | 2.797 秒 | 解码 155520 样本、6480 ms |
| PCM | 311040 | `sha256:9934c6b42648ab88457a3b69a53abdfc7f337f1a2defa761590abf86c985dd9b` | 2.703 秒 | 恰好 155520×2 字节 |

三条命令的源 PCM 均为 155520 样本、6480 ms，生成区间均为 `[0,155520)`；各自使用独立
submit intent、引擎请求号、对象版本和回执。三次重启后回执都保留，原身份重放都保持
`unknown_quarantined`，预算计数不变且没有停止证据行。MP3 的编码延迟/填充没有改变
回执声明的源样本事实，解码回读也没有出现时长漂移。

## 验证与边界

- `uv run pytest -q tests/unit --no-cov`：3869 passed。
- `uv run ruff format --check .`、`uv run ruff check .`、`uv run mypy`：通过。
- `uv run lint-imports`：7 条依赖边界全部通过。
- `make contracts-check`：32 份机器契约通过。
- 真实探针：WAV/MP3/PCM 共 3 passed，容器停止后 `Running=false`，网络已删除，
  受保护 35B 未变化。

本证据不证明 Base、VoiceDesign、音色克隆、流式、长文本切分、公开路由/Job/发布/下载、
SDK/C 消费、A 的预算结算、单请求取消/停止/资源释放双事实、物理多节点竞争或生产许可。
只有后续在这些范围内取得对应证据，才能勾选相应 AC。
