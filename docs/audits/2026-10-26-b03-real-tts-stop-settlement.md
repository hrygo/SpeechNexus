---
title: "B03 真实 CustomVoice 代次鉴权、取消与整代次停止结算"
status: in_progress
version: "0.1.0"
date: 2026-10-26
---

# B03 真实 CustomVoice 代次鉴权、取消与整代次停止结算

2026-10-26（Asia/Shanghai），在 main `2300310` 基础上，仅扩充控制器原生鉴权模板的
TTS 预热路径后，完成真实 CustomVoice 取消、整代次停止、默认网关恢复及 A 原租约事务联验。
成功 run 为 `20261026T064848Z-87b6ef51`，不是公开业务链或物理集群验收。

## 变更与固定条件

原 `vllm_api_key_v1` 模板只允许 `/v1/audio/transcriptions` 预热，无法装配同样需要
代次凭据的 TTS。现在增加 `/v1/audio/speech`，复用原派生 key、实际镜像/argv/环境/身份
核对和有界预热；不增加认证模式、数据库表、事务端口或预算账本。只通过 health、任意路径、
CLI key/config/root-path/middleware 覆盖仍被拒绝。旧控制器拒绝新 TTS 模板，部署先升级控制器。

- Linux ARM64 / GB10 单主机，固定镜像
  `sha256:5a12eac1a2dd3540a6ec29e42ef0dd5286c75ee07f68e497cb5f0df6e1311a52`；
  vLLM 0.32.0 / vLLM-Omni 0.32.0rc1，真实 Qwen3-TTS 1.7B CustomVoice。
- 模型 13 个文件共 4,520,217,479 bytes 已逐文件记录 SHA-256。该本机指纹用于固定本轮
  字节，不独立认证上游 revision，也不授予许可或容量资格。
- 模板显式执行 `vllm-omni serve --omni`，只读模型、内部 bridge 网络、不发布宿主端口；
  `gpu-memory-utilization=0.06`、`max-model-len=4096`，Memory/MemorySwap 均为 16 GiB。
  这不是 GPU 资源隔离或 peak→steady 降额资格。
- 默认控制器 `create_app(settings=...)` 使用实际 Docker、SQLite、观察循环和回环 TLS；
  默认网关使用原文件 TTS 工厂、worker、恢复循环、非 owner PG 角色及隔离 MinIO 桶。
  没有注入 controller runtime、ledger、合成器或执行 factory。
- 模板由控制器注入目标代次派生 key，网关仅获得该目标的描述。两次受保护路径预热使用
  同一凭据；真实引擎对缺失凭据和另一 generation 派生的凭据分别返回 401。
- 执行输入为 380 字符合成文本，显式 planner 上限 400；未读取私人语料。模板/绑定、
  readiness 资格、Job/准入及 runtime stop 意图生产者仍为 fixture。停止通过真实控制器
  执行，但不冒充 A 已持久化该 runtime 意图或完成生产调度。

## 失败保留与预检

无模型预检先暴露实验配置缺少集成 fixture 要求的 InnoDB DSN，随后暴露 planner 超出契约
上限；只修驱动，未放宽契约或产品校验。修正后真实 PG 提交、取消、零结果和预算保留通过。

首轮真实 run `20261026T064343Z-18bbd185` 已完成两次预热（195.495 秒）、取消/原身份重放、
零结果/预算不变、提前结算拒绝，以及控制器退出与资源释放（11.646 秒、exit 0）。
随后驱动错误地从 `submit_intents` 查询 `stop_evidence_digest`；该值实际来自独立停止事实表，
查询报 `UndefinedColumn`，整轮退出 1。网关登记与 A 结算没有完成验证，不记为通过。
失败报告和清理结果保留；修正查询、补无模型预检后才执行成功轮。

## 成功轮与预算顺序

| 事实 | 结果 |
| --- | --- |
| 控制器 prepare 至两次真实鉴权预热完成 | 195.564 秒 |
| 取消前原生 running/waiting gauge 汇总 | 1 / 1 |
| 两次网关 abort 回执合计 | 0.020 秒；不是 GPU 停止延迟 |
| 取消及两次原请求重放 | unknown/quarantined、原网关 engine request ID 不变 |
| 取消后私有回执/对象与停止事实 | 无回执、0 对象、0 停止事实；预算不变 |
| A 提前 release | `execution_cleanup_pending` |
| 整代次控制器停止 | 11.839 秒、exit 0，退出与资源释放双事实 |
| 默认网关恢复 | 唯一 `instance_terminated_by_controller`，执行 `aborted/complete` |
| B 登记停止事实后 | A 的预算仍不变 |
| A 原 `PostgresLeaseTransactions.release_lease` | 首次 committed，重复 replayed，账目不再变化 |
| 持久检查点重新打开 | SQLite released 检查点与原容器 ID 保留 |

running/waiting 是多阶段引擎的汇总观察，不作为请求级 abort 回执，也不证明资源释放。
本轮通过批准的整代次停止终结执行，未取得单请求 GPU 停止/释放资格。

| 时点 | host held | 目标实例 held slots | workspace held attempts |
| --- | --- | --- | --- |
| 提交前、取消/重放后、B 停止事实登记后 | 16 GiB + 1 MiB | 1 | 1 |
| A 原租约释放后及重复释放后 | 16 GiB | 0 | 0 |

16 GiB 基础预留由实验 fixture 设置，本轮未调用 A runtime base 结算，也没有声称全部预算
归零。原租约从 reserved/pending/v1 变成 released/complete/v2；private staging 始终无对象。

## 验证、清理与证据

- 先红后绿：新增 TTS 鉴权路径在旧实现上 8 项失败，扩充后控制器 291 项单元通过。
- 控制器、TTS 命令/工厂/适配器/worker 与控制器契约共 393 项定向测试通过。
- 停止事实消费、默认恢复和原租约释放的隔离 PG/InnoDB 集成共 77 项通过，无跳过。
- Ruff format/check、mypy、7 条依赖边界、32 份机器契约通过。上述代码检查不等于模型资格。
- 成功轮探针 1 passed、退出 0；目标容器不存在，内部网络已删除。35B、embedding、reranker
  前后运行状态及 StartedAt 均相同。失败轮也没有遗留目标或网络。

私有驱动、SQLite 和原始报告保留在忽略目录 `.runtime/probes/real-tts-stop/`。成功轮
`source-fingerprint.json` 固定实际源码及唯一产品补丁，避免把未提交实验误称为已验证裸 main。
不将根密钥、代次凭据、模型路径、请求正文或音频加入 Git。

| 证据 | SHA-256 |
| --- | --- |
| 首轮失败 report | `285fa8b944b7df1763d7d0ae07c912df751d3bffec5316c41b22b81a945059e3` |
| 成功 report | `333fce90539e671f9b1a006f1a61ac53d3390e40036ddc8a32d37bb700c9d2d5` |
| 成功 runner-report | `0a92aa0cbc89a33a093e76859acb15afac83f999814bb6247f4f44e191830e05` |
| 成功冻结驱动 | `9c32cf90ae373712401bda45453fb8f65ed5da36285e51b9e408fb0ca7db2a51` |
| 成功源码指纹 | `ea9ff7590a937733316f0352836bf431cd66feccc222ceed8e6268b31cba1afa` |
| 模型本机指纹 | `ce40cb39df89d6df85db5d15959373e309330115af10a035cf3d4b9f9d8b82ec` |

下一步仍是 A 的权威配置/readiness、正式 Job/准入/runtime 意图/发布，以及 SDK/C 的公开链。
控制器真实故障重启、实例 base 结算、单请求释放、Base/VoiceDesign、长文本、质量/容量、
混合负载与物理多机分别待验。本轮不关闭 #29/#30/#142、Gate 或 B00–B07 Goal。
