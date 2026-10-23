---
title: "B00 MegaCube 环境与能力缺口前检"
status: in_progress
version: "0.2.0"
date: 2026-10-24
---

# B00 MegaCube 环境与能力缺口前检

本文是团队 B 对 [B00 #27](https://github.com/hrygo/SpeechNexus/issues/27) 的记录，对应[团队 B 目标](../plans/2026-10-23-team-b-goal.md)与[三团队实施方案](../plans/2026-10-23-three-team-delivery-plan.md)的 S0 要求。基线为 `main` 的 `92ba52b`。

执行范围：只读环境前检；经用户授权的两项资源调整（停止 `qwen3-asr.service`、调低 embedding/reranker 显存比例）；经用户授权的制品下载（ModelScope 与 Hugging Face）；经用户授权的一次真实引擎生命周期探针（Qwen3-ASR 0.6B，合成音频）。用户明确指示保留 `cube-ccba-qwen36-35b-nvfp4`，本次未改动该服务。

测量与操作时间：2026-10-24 00:34–01:20（Asia/Shanghai），即 2026-10-23T16:34–17:20Z。

结论只以本文列出的命令输出为限。主机具备 GB10 级别的 aarch64 GPU 环境，已取得首个真实引擎加载、推理与停止证据；但**没有**真实语音质量、容量、实时性或目标拓扑结论，本文件不构成 G0 通过。

## 1. 主机与运行时事实

| 项 | 实测值 | 采集方式 |
| --- | --- | --- |
| OS | Ubuntu 24.04.5 LTS | `/etc/os-release` |
| 架构 / 内核 | aarch64 / Linux 7.0.0-1019-nvidia | `uname -srmo` |
| CPU | 20 × ARM（Cortex-X925 / Cortex-A725） | `lscpu` |
| GPU | NVIDIA GB10，PCI `0000000F:01:00.0`，MIG 不适用 | `nvidia-smi` |
| 驱动 / CUDA | 580.178.04 / CUDA 13.0（`nvcc` V13.0.88，`/usr/local/cuda-13.0`） | `nvidia-smi`、`nvcc --version` |
| 内存 | 121 GiB 统一内存；空闲态可用 43–49 GiB；swap 15 GiB | `free -g` |
| 磁盘 | `/dev/nvme0n1p2` 3.7 TiB，可用约 3.1 TiB | `df -h` |
| 容器运行时 | docker，当前用户属 `docker` 组；GPU 服务由**系统级** systemd 单元拉起 | `id -nG`、`systemctl cat` |
| 项目隔离服务 | `speechnexus-postgres`(127.0.0.1:55432)、`speechnexus-minio`(127.0.0.1:59000) 用户级 systemd；`speechnexus-coordination-mysql` 容器(127.0.0.1:55434) | `systemctl --user list-units`、`docker ps` |

GB10 为统一内存架构，`nvidia-smi` 的显存总量列报为 `Not Supported`，单进程占用为近似值；容量判断以系统内存与实测加载峰值为准，不能引用外部标称值。

## 2. 既有 GPU 运行态与授权释放结果

| 容器 | 管理方式 | 用途 | 端口 | 释放前占用 | 本次处置 |
| --- | --- | --- | --- | --- | --- |
| `qwen3-asr` | `qwen3-asr.service`（系统单元，`Restart=always`） | Qwen3-ASR 0.6B 转写 | 127.0.0.1:8020 | 3629 MiB | 经授权 `disable --now`：停止并禁止自启 |
| `qwenpaw-embedding` | `qwenpaw-embedding.service` | 文本 embedding（0.6B pooling） | 127.0.0.1:8012 | 2373 MiB | 经授权 `--gpu-memory-utilization` 0.03 → 0.02，已重启验证 |
| `qwenpaw-reranker` | `qwenpaw-reranker.service` | 文本 rerank（0.6B） | 127.0.0.1:8013 | 3723 MiB | 同上 |
| `cube-ccba-qwen36-35b-nvfp4` | 无 systemd 单元（compose 项目，`restart=unless-stopped`） | 35B 文本 LLM | 0.0.0.0:8001 | 47192 MiB | **用户指示保留，未改动** |

授权动作与证据：

1. `systemctl disable --now qwen3-asr.service`，2026-10-23T16:40:17Z。单元 `inactive` + `disabled`，容器被移除，8020 关闭，GPU 计算进程 4 → 3。容器、镜像与权重未删除；恢复命令为 `systemctl enable --now qwen3-asr.service`。
2. embedding / reranker 单元文件 `0.03` → `0.02`，备份在 `/root/b00-backups/*.service.orig`，`daemon-reload` 后逐个重启。`/health` 均 200（19 s / 42 s），功能探针通过（`/v1/embeddings` 1024 维；`/v1/rerank` 相关文本 0.9996、无关 0.0004）。占用 2373 → 2257 MiB、3723 → 2361 MiB。
3. `0.02` 只验证了健康与单次功能请求；长时运行与并发未验证。

## 3. B00 制品清单与获取记录

全部制品下载位置为 `/home/huangzhonghui/speechnexus-models/`，权重与镜像均不进入 Git。摘要为 `sha256` 十六进制前缀。

| 制品 | 来源 | 本地大小 | 权重摘要 | 许可（模型卡） |
| --- | --- | --- | --- | --- |
| `Qwen3-ASR-0.6B` | ModelScope `Qwen/Qwen3-ASR-0.6B`（更早取得） | 1.8 GB | 未重算 | apache-2.0 |
| `Qwen3-ASR-1.7B` | ModelScope `Qwen/Qwen3-ASR-1.7B` | 4.4 GB（双分片） | `a4cd1f1a…`、`6e0b9d9e…` | apache-2.0 |
| `Qwen3-TTS-12Hz-1.7B-CustomVoice` | ModelScope 同名仓库 | 4.3 GB | `38b1d597…` | apache-2.0 |
| `Qwen3-TTS-12Hz-1.7B-VoiceDesign` | ModelScope 同名仓库 | 4.3 GB | `391e8db2…` | apache-2.0 |
| `Qwen3-TTS-12Hz-1.7B-Base` | ModelScope 同名仓库 | 4.3 GB | `38fc7fc5…` | apache-2.0 |
| `Qwen3-ForcedAligner-0.6B` | ModelScope `Qwen/Qwen3-ForcedAligner-0.6B` | 1.8 GB | `47831d0e…` | apache-2.0 |
| `Nemotron-3-Diarization` | Hugging Face `nvidia/Nemotron-3-Diarization`（16 个文件） | 657 MB | `24c28c0e…`（`model.safetensors`） | openmdw-1.1 |
| `silero_vad.onnx` | GitHub `snakers4/silero-vad` @ `bfdc0193023f121ea5b3cc7b176dbed570a68a59` | 2.2 MB | `1a153a22…` | MIT |
| ARM64 vLLM 服务镜像 | 本机既有 `local/qwen3-asr-vllm:0.28.0-arm64` | 31.4 GB | 镜像 `sha256:61cc94fe…` | vLLM v0.28.0（buildkite 5584，commit `2cf0a691…`） |
| `vllm/vllm-omni` | Docker Hub（`linux/arm64` 清单存在） | 拉取中 | 待记录 | 见镜像标签 |

未随权重分发的 `LICENSE` 文本没有落到本地；上表许可来自模型卡 front matter。仓库整体 LICENSE 决策仍待定，`openmdw-1.1`、`apache-2.0` 与 MIT 的合并口径需要单独确认后才能用于对外发布资格。

使用形态上有一处关键约束：Qwen3-TTS 模型卡写明当前 vLLM-Omni **只支持离线推理**，在线服务尚未提供。因此 B03/B05 的 TTS 路径不能假设存在 OpenAI 兼容服务端点，需按受控离线批处理形态设计适配器，并在上游提供在线服务后再评估。

## 4. 首个真实引擎生命周期探针（Qwen3-ASR 0.6B）

目的：验证本机能否真实加载、服务并停止一个受控引擎实例，并采集"执行层 + 资源层"双证据。使用合成音频（440 Hz 正弦与等长静音），不含任何私有音频。探针容器独立命名 `b00-probe-asr`，未触碰既有服务。

运行参数：`local/qwen3-asr-vllm:0.28.0-arm64`，`--memory=10g --memory-swap=10g --gpus all --shm-size=4g --cap-drop=ALL --security-opt=no-new-privileges --read-only` 与 tmpfs 挂载，权重只读绑定，`--publish 127.0.0.1:8030:8000`，`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`；引擎参数与既有生产单元一致（`--dtype bfloat16 --gpu-memory-utilization 0.05 --max-num-seqs 2 --max-model-len 8192 --limit-mm-per-prompt {"audio":1} --enforce-eager`）。

| 阶段 | 实测 |
| --- | --- |
| 容器启动 | 2026-10-23T17:08:20Z |
| 就绪 | `/health` 200，启动后 96 s；引擎日志 `init engine (profile, create kv cache, warmup model) took 48.93 s` |
| 服务期占用 | 容器内 `docker stats` 4.109 GiB / 10 GiB 上限；进程侧 5479 MiB；系统已用 78 → 87 GiB，可用 43 → 34 GiB |
| 请求 1：4 s 正弦音 | HTTP 200，`text: ""`，0.110 s，`usage {"type":"duration","seconds":4}` |
| 请求 2：4 s 静音 | HTTP 200，`text: "Ahora."`，0.081 s（**非语音幻觉**） |
| 请求 3：30 s 正弦音 | HTTP 200，`text: ""`，**35.06 s**（超线性，见下） |
| 停止 | `docker stop -t 45` 用时 3 s，退出码 0，`FinishedAt 17:11:37Z` |
| 停止后证据 | 8030 关闭；该 `VLLM::EngineCore` 进程从计算进程列表消失；系统已用 87 → 72 GiB，可用 34 → 49 GiB |

两个必须如实记录的现象。其一，4 秒静音被识别为 `"Ahora."`，说明该组合在无语音输入上会产生幻觉文本，B03 的"无语音"路径不能只依赖引擎输出，需要显式门禁。其二，30 秒非语音输入耗时 35.06 s，明显超过实时，且与 4 秒样本的 0.11 s 不成比例；本次未定位原因（未测真实语音、未设 token 上限、未做重复采样），因此不能作为性能结论，也不能据此推断真实音频的延迟。两条都属于后续探针的输入条件。

停止阶段引擎日志出现 `Process manager: force killing remaining process EngineCore pid 239`：vLLM 的停止路径会强杀引擎子进程。因此"进程终止"这一执行层证据与"资源回落"这一资源层证据必须分别采集，不能只看 `docker stop` 返回码。

## 5. 逐能力缺口矩阵

候选组合见 [ADR 0002](../decisions/0002-speech-models-engines-and-loading.md)。"探针"一列指真实引擎探针，目前只有 ASR 0.6B 生命周期一项已执行。

| 能力 | 候选模型 / 引擎 | 本机制品 | 探针 | 当前判定 |
| --- | --- | --- | --- | --- |
| 文件 ASR / 完整段复核 | Qwen3-ASR-1.7B + vLLM | 就绪（摘要已记录） | 未执行 | 可探针 |
| 实时 ASR | Qwen3-ASR-0.6B + vLLM | 就绪 | 生命周期已探；实时性未测 | 部分证据 |
| 预设音色合成 | Qwen3-TTS-12Hz-1.7B-CustomVoice + vLLM-Omni | 就绪 | 未执行 | 可探针（离线形态） |
| 描述式音色设计 | Qwen3-TTS-12Hz-1.7B-VoiceDesign + vLLM-Omni | 就绪 | 未执行 | 可探针（离线形态） |
| 样本克隆 / 复用合成 | Qwen3-TTS-12Hz-1.7B-Base + vLLM-Omni | 就绪 | 未执行 | 可探针（离线形态） |
| VAD | Silero VAD ONNX + ONNX Runtime CPU | 就绪 | 未执行 | 可探针（CPU） |
| 强制对齐 | Qwen3-ForcedAligner-0.6B | 就绪 | 未执行 | 可探针 |
| 匿名分人 | Nemotron-3-Diarization | 就绪 | 未执行 | 可探针（需运行时依赖） |
| 提交屏障 / 停止事实 / 输出区间账本 | 网关自有实现 | 不依赖外部制品 | 见第 7 节 | 首个单元已交付 |

## 6. 停止与资源释放证据草案

团队 B 的"停止"判定采用双证据门槛，缺一不成立：

- 执行层证据：引擎进程与 CUDA 上下文终止、容器退出状态、监听端口关闭、无残留计算进程。
- 资源层证据：统一内存 / GPU 占用回落，实例 generation 终止，相关预算可回收。

明确不采信：HTTP 连接断开、请求被取消、TTL 到期、只写入更大的 fencing token、以及"已发出 stop 命令"本身。结果未知时继续隔离并占用预算。

第 4 节给出了第一条来自真实引擎的双证据：容器退出码 0、端口关闭、引擎进程消失（执行层），系统可用内存 34 → 49 GiB（资源层）。它证明本机可以采集这两类证据，但停止动作是人工 `docker stop`，不是 SpeechNexus 网关的 stop 路径，因此不能替代 B01/B02 对提交屏障与实例生命周期的验收。

manifest 草案（当前可确定的值已标注，其余留待实测）：

| 契约 | 相关必填字段 | 当前状态 |
| --- | --- | --- |
| `contracts/model-binding.schema.json` | `binding_id`、`revision_id`、`logical_model`、`tasks`、`engine`、`artifact_set`、`execution_recipe`、`parameter_domains`、`release_tier`、`publication` | `logical_model`、`engine`、`artifact_set` 摘要已可得；其余待探针 |
| `contracts/capacity-manifest.schema.json` | `manifest_id`、`binding_revision_id`、`pool_id`、`service_class`、`management_mode`、`loading_policy`、`topology_ref`、`stage_graph`、`resource_envelope`、`rate_and_segment_limits`、`cancellation_and_fencing`、`qualification` | `loading_policy`、`management_mode` 方向明确；`resource_envelope` 有单实例稳态与峰值样本；限流待测 |
| `contracts/model-instance.schema.json` | `instance_id`、`host_node_id`、`gateway_id`、`pool_id`、`generation`、`config_digest`、`state`、`readiness`、`observed` | `host_node_id` / `gateway_id` 本机取值可得；`config_digest` 待定义 |
| `contracts/qualification-report.schema.json` | `report_id`、`binding_revision_id`、`capacity_manifest_id`、`release_tier`、`topology`、`software`、`test_suite`、`scenarios`、`overall_status`、`limitations`、`evidence_digest` | 待探针与联调后填写 |

## 7. 不依赖 GPU 的可开工单元（B01 首个单元已交付）

B01 的首个闭合单元已经实现并通过门禁，PG 落库仍待 A01 的迁移与端口：

| 文件 | 内容 |
| --- | --- |
| `execution-gateway/src/speechnexus_gateway/domain/canonical.py` | RFC 8785 子集的规范化 JSON 与 `sha256:` 摘要；键按 UTF-16 码元排序，数字按 ECMAScript `Number::toString` 输出；超出子集抛错，而不是给出可能与其它实现不一致的摘要 |
| `execution-gateway/src/speechnexus_gateway/domain/submission.py` | 提交身份、执行/清理状态矩阵、dispatch 证据与停止证据规则；实现契约要求的 `submitting` / `unknown_quarantined` 继续持有容量，以及 `failed + not_required` 必须无 dispatch 时间与引擎请求号 |
| `execution-gateway/src/speechnexus_gateway/domain/ports.py` | `SubmissionLedger`、`EngineDispatchPort`、`EngineObservationPort` 三个端口 |
| `execution-gateway/src/speechnexus_gateway/application/submit_execution.py` | 提交与停止服务：先持久化再外发；同键同摘要重放不再次 dispatch；同键异摘要冲突；确认丢失进入 `unknown_quarantined` 且不重发；只有 verified 停止证据才释放容量 |
| `tests/unit/gateway/test_canonical_json.py`、`tests/unit/gateway/test_submit_execution.py`、`tests/fake/execution.py` | 51 个用例与内存 ledger / 脚本化引擎 double |

验证：`uv run pytest tests/unit tests/contract -q` 通过 286 项；`uv run ruff format --check .`、`uv run ruff check .`、`uv run mypy`、`uv run lint-imports` 全部通过。该单元不含 HTTP 路由：执行控制接口的边界类型等待 A00 的生成流水线，避免手写第二套字段。

## 8. 未验证项与阻塞

- 真实语音质量、准确率、长文件与实时性均未验证。本轮只有正弦与静音合成音频，且非语音路径出现幻觉与超线性延迟。
- TTS、音色设计、克隆、VAD、对齐、分人均未运行探针；`vllm/vllm-omni` 镜像尚在拉取，其离线推理依赖也未验证。
- 提交屏障、控制器生命周期、输出区间账本只完成首个领域单元；PG 适配器、隔离双库协调验证与 A01 端口对接未开始。
- 实例 generation、fencing 与迟到结果场景只有 fake 层面覆盖，无真实引擎多实例竞争证据。
- 35B LLM 占 47192 MiB（约 91% 的 GPU 侧占用），按用户指示保留；可并行的探针规模因此受限。
- 许可：`apache-2.0`（Qwen 系列）、MIT（Silero VAD）、`openmdw-1.1`（Nemotron-3-Diarization）需并入仓库许可决策；本地无 LICENSE 文本副本。
- 目标拓扑、跨主机联调与物理集群资格未启动；单 MegaCube 多进程仍只应标为逻辑集群。

## 9. 下一步

1. 用授权语料或合成**语音**复核 ASR 0.6B/1.7B：确认静音幻觉与 30 s 非语音延迟是否为路径问题，并设置 token 上限与超时。
2. 用已就绪制品跑 VAD、对齐与分人探针（CPU/GPU 分别记录），补齐逐能力支持矩阵。
3. 在 vLLM-Omni 离线形态下验证 TTS 三模型的加载、输出格式与停止行为，并据此确定网关的 TTS 适配器形态。
4. 继续 B01/B02 的无 GPU 单元：PG 适配器等待 A01，控制器 fake 与 generation 场景可先行。
5. 需要用户裁决：`openmdw-1.1` 等许可口径、可用的授权语音语料、是否放宽 35B 占用以并行探针。

[跟踪矩阵](../plans/2026-10-23-implementation-tracking.md) · [团队 B 目标](../plans/2026-10-23-team-b-goal.md) · [三团队实施方案](../plans/2026-10-23-three-team-delivery-plan.md)
