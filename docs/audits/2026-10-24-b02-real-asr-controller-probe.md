---
title: "B02 有界控制器与真实 ASR 生命周期探针"
status: in_progress
version: "0.1.0"
date: 2026-10-24
---

# B02 有界控制器与真实 ASR 生命周期探针

本轮在 `main@2874c839f81a52cd079654372785a627dbea59a0` 上实施固定容器内存上限、
HTTP 健康就绪与节点隔离，并通过真实 Docker API 串行运行 Qwen3-ASR 0.6B / 1.7B。
控制器应用服务、SQLite 回执和容器均为真实实现，输入全部为本机生成的合成语音、静音和正弦音。
这证明控制器到两个 ASR 引擎的本机生命周期路径，**不证明**公开文件语音闭环、A 的预算控制循环、
实时性、质量/容量资格或物理集群验收。

## 1. 环境、制品与边界

- Ubuntu 24.04.5 aarch64、NVIDIA GB10、驱动 580.178.04、CUDA 13.0、121 GiB 统一内存。
- 本地固定镜像 `local/qwen3-asr-vllm:0.28.0-arm64`，image ID
  `sha256:61cc94fec25198aeefcd93b96088a8e8db106d61d834a36d3b89ba7bf5b10765`；
  引擎实际日志报告 vLLM 0.28.0，镜像内 Python 3.12，控制器运行于仓库 Python 3.14.9。
- 0.6B / 1.7B 的容器 Memory/MemorySwap 分别固定为 10 / 12 GiB。探针的
  `reserved_memory.peak` 与 `steady` 都保留完整上限，不虚构可降低的稳态资格。
- 单 GPU、bf16、`gpu_memory_utilization=0.05`、`max_num_seqs=1`、
  `max_model_len=8192`、eager；每请求 `max_completion_tokens=64`、language=en、
  temperature=0，并启用请求 ID 回显。
- 自定义 bridge 网络经 daemon inspect 确认为 `Internal=true`；引擎没有宿主 PortBindings。
  探针只访问该容器 IP，模型通过模板只读绑定；没有上传音频到外部服务。
- 每次用唯一 node/instance/generation 标签定位和清理自己的容器；无 Scheduler 数据库写入。
- `cube-ccba-qwen36-35b-nvfp4` 始终运行，前后 `StartedAt` 都是
  `2026-10-23T03:49:18.877205405Z`。结束后 GPU 仅保留该服务及既有 embedding/reranker
  三个进程，临时 ASR 容器和临时内部网络均已删除。

探针后重新计算了实际权重 SHA-256，不用下载记录代替当前文件核验：

| 模型 | 权重文件 | SHA-256 |
| --- | --- | --- |
| 0.6B | `model.safetensors` | `79d6cbd4c98c7bbffe9db2edac07f56cd6637d0d5944b27f6c2b8353840323ea` |
| 1.7B | `model-00001-of-00002.safetensors` | `a4cd1f1a04d90b757dc7f7dd26254e69a013b19e80efe590a83c6a3bde8608d6` |
| 1.7B | `model-00002-of-00002.safetensors` | `6e0b9d9e09e2e0238e7ef3cc8a484ab387e91b90f1900bedf88bc92d7929ccfc` |

这些摘要不是完整模型 revision/许可证锁；模型发布与资格仍需 B00/B07 完整制品清单。

## 2. 实际测量

合成语音由 `espeak-ng` en-us、155 words/min 在本机产生，共 4.848 秒；两个模型消费相同
13 词参考句。WER 只忽略大小写与标点；不把这一句的结果外推为中英文真实语音质量。

| 项 | ASR 0.6B | ASR 1.7B |
| --- | --- | --- |
| 首次控制器观察 | loading | loading |
| `/health` 200 后 ready | 90.627 s | 100.730 s |
| ready 时 daemon 内存观察 | 5,352,329,216 bytes | 5,927,395,328 bytes |
| 系统 MemAvailable：前 / ready / 后 | 52.242 / 42.813 / 52.316 GB | 52.443 / 41.751 / 52.717 GB |
| 合成语音首次 / 重复请求 | 0.185 / 0.148 s，WER 均为 0 | 0.382 / 0.349 s，WER 均为 0 |
| 4 秒静音 | 0.073 s，产生非空文本 | 0.155 s，产生非空文本 |
| 30 秒 440 Hz 正弦音 | 29.469 s，产生非空文本 | 31.422 s，产生非空文本 |
| 请求 ID | 四次均原值回显 | 四次均原值回显 |
| 重建服务/回执连接后的 prepare | 原意图重放，容器仍只有 1 个 | 原意图重放，容器仍只有 1 个 |
| 停止 | 2.106 s，exit 0 | 2.098 s，exit 0 |
| 退出与删除后不存在的双证据 | 齐全 | 齐全 |
| 紧急清理 / 容器遗留 | 不需要 / 0 | 不需要 / 0 |

GB 为十进制展示；模板上限的 GiB 为二进制。daemon 读数是观察值；cgroup v2 未提供
`max_usage` 时使用当前 usage，不能将该数字冒充完整加载峰值或 GPU 峰值。系统内存差也包含
缓存与后台变化，不能用作严格容量结论。

“重建”指同一探针进程关闭并重开 SQLite 连接、重建控制器服务；不是杀死运行进程、真实故障接管
或跨节点恢复。此前合成 CPU 容器的控制器 HTTP/进程重启证据独立记录在
[HTTP 外壳探针](2026-10-24-b02-controller-http-shell-probe.md)。

**未解决**：两个模型在无语音输入上都有幻觉；长非语音即使限制 64 个输出 token，仍接近或超过
输入时长。因此 B03/B06 必须接入显式无语音门禁，不能依赖引擎输出为空，也不能把这些测量当作
实时 P95、并发或长文件资格。探针尚未验证请求级 status/abort 和生成输出上界。

## 3. 启动失败与资源诊断

早期一轮启动未就绪，容器已清理，但当时未保存 vendor 日志，不能精确归因。探针随后增加失败时
保存本地诊断和 JSON 结果的能力；原始引擎日志只留在忽略的 `.runtime/`，不进入 Git。

下一轮保留的日志在加载权重前确认了 `torch.accelerator.get_memory_info` 的 CUDA OOM。
独立、短时、10 GiB 上限的 CUDA 上下文探针成功，但 CUDA 只报告约 4.03 GB 空闲；系统
MemAvailable 同时约 52.45 GB。两个读数不能互相替代。

仅对未运行的本地语音模型 `.safetensors` 使用 `POSIX_FADV_DONTNEED`，建议内核回收干净文件
缓存：范围 13 个文件 / 27.08 GB，MemFree 4.576 → 15.125 GB，Cached 44.733 → 34.183 GB；
MemAvailable 基本不变。没有执行全局 drop_caches、修改内核参数、删除权重或停止既有服务。
随后两个固定上限的真实 ASR 探针成功。

这是本轮前后测量，不能据此宣称所有 CUDA OOM 都是文件缓存导致。控制器不自动回收宿主缓存，
不把 CUDA 初始化失败伪装成 ready，也不通过取消容器内存限制绕过问题。

## 4. 可重复入口与兼容边界

入口：

```bash
uv run python scripts/dev-env/asr-controller-probe.py \
  --templates-file <批准模板.json> \
  --template-id <批准模板ID> \
  --served-model-name <模板声明的模型名> \
  --evidence-directory .runtime/probes/asr-controller
```

前置：明确 GPU 操作授权、固定本地镜像与模型、`espeak-ng`、本机 Docker、明确声明且
`Internal=true` 的自定义 bridge 网络。模板 schema/范围、升级方式见
[控制器说明](../architecture/node-controller.md#2-受控容器-adapter-与批准模板)。
每次执行前核对其他模型与内存，不并发运行多个 GPU 探针。

工具只产生合成输入，报告只输出摘要、状态和测量，不输出转写正文、凭据或宿主模型路径。
SQLite 和输入临时文件自动删除，报告与本地诊断按唯一运行 ID 保留；失败时仅按本轮唯一标签
强制清理自己的容器，紧急清理不算控制器停止证据。调用方仍需删除自己创建的空临时网络。

本轮不改 wire、数据库、共享生成类型或 A 的预算权威。模板新增必填
`container_memory_limit_bytes`；旧配置启动失败而非静默无限额。重启发现无限额/漂移容器时
quarantine，仍可按原身份停止；新 generation 需重新创建，不能靠更新模板冒充旧容器已受限。
GPU/统一内存隔离与 A 的真实 warmup/资格、peak→steady、lease 清理另行联验。

## 5. 任务状态更正

按用户要求核对了全部 38 个历史 Issue 的状态与正文。更正范围：
总控 #6，Epic #7/#8/#9/#10/#12，以及 #19/#20/#21/#26/#27/#28/#29/#31。
保留原验收条件，只勾选已合并且有证据的分段任务；当前分支未合并内容在 #29 明确标为进行中。
未实施的任务和未联合验收的 Gate 保持未完成；工作包完成仍为 0/25，Gate 仍为 0/6。

团队 B 开工前更新标签、正文与本轮单元，交付后再同步证据和勾选状态；不能以追加评论代替正文
维护，也不能将已交付子单元误写为整个父包完成。

## 6. 本机回归

- `uv run pytest -m 'not integration' --cov --cov-report=term-missing:skip-covered`：
  1,303 passed，覆盖率 80.34%，保持 80% 门槛；另有一个既有 SQLite ResourceWarning。
- 同步 main 的 #72/#73（`2ba1555`）后重新执行同一完整非集成检查：
  1,363 passed，覆盖率 80.44%；控制器实现与真实 ASR 探针路径没有上游冲突。
- controller 单元与契约针对性验证：150 passed。覆盖 HTTP 503/重定向/超时不 ready、固定
  内存/禁止额外 swap、host/unified 预留覆盖、其他 node 不误停、旧无限额/网络漂移实例隔离且
  可安全停止。
- 当前代码重新运行真实 CPU HTTP 探针：health 200、无凭据 401、外节点 409、
  prepare/stop 均 200、SIGTERM exit 143、2.118 s 停止、独立进程重启后回执保留、零遗留。
- Ruff format/check、mypy、7 条依赖边界、28 份机器契约、Compose 配置和四个 Python
  包构建通过；文档链接与 diff 另核对。
- 本轮不改协调存储，未重复本机整套 PG/InnoDB/MinIO 集成；远端检查结果在对应 PR 记录。
  之前 #71 的协调/区间合成证据不能替代真实 A runtime 联调。

[审查索引](README.md) · [B00](https://github.com/hrygo/SpeechNexus/issues/27) ·
[B02](https://github.com/hrygo/SpeechNexus/issues/29) · [团队 B 目标](../plans/2026-10-23-team-b-goal.md)
