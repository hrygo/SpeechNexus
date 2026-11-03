---
title: 受控 TTS 验收与资源收尾
status: incomplete
date: 2026-11-03
---

# 受控 TTS 验收与资源收尾

首次窗口未取得可用声音资格，也未完成公开同步 TTS 验收。固定候选为
`90f53c4ead3236ccba61bc353259dc7f05e3a179`；全部 2002 个 Git blob 与远端隔离源码一致，
固定模型制品的离线核验通过。这些检查不能替代真实合成。

## 实际结果

| 项目 | 结果 |
| --- | --- |
| 模型 | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice`，Vivian；revision `0c0e3051f131929182e2c023b9537f8b1c68adfe` |
| 原始 artifact pin SHA-256 | `e6c898aab968ddadae2ce330437032265b5d8192c23ec4f10c4d1b225296e779` |
| 镜像 | `sha256:5a12eac1a2dd3540a6ec29e42ef0dd5286c75ee07f68e497cb5f0df6e1311a52` |
| 新增实例 | 单实例、generation 1，已核对内存硬上限 12 GiB |
| prepare 开始 | 2026-11-03 00:18:45.501760 UTC |
| prepare 终态 | 00:21:58.144257 UTC：`failed` / `instance_prepare_failed` / `stopped` |
| 原停止意图 | 00:22:16.582013 UTC：`succeeded`，容器退出和资源释放双事实均为 true，退出码 1 |
| 控制器内存观察字段 | `observed_peak_memory.unified_memory_bytes=3226693632`；不是最终 OOM 原因或真实峰值的充分证据 |
| 实际测量 | 尚未进入 native profile 测量；无 WAV、无标准 SDK 成功响应 |
| 正式账本 | 来源批准 1、binding 草稿 1；声音批准、声音资格、publication、runtime head、admission attempt、lease 均为 0 |

原始请求/响应的 JSON 内容、各原文件摘要及清理结果保存在
[结构化证据](evidence/2026-11-03-tts-prepare.json)。这是失败与物理停止证据，
不能计作 READY、声音资格、正式预算结算或公开业务链通过。

## 诊断边界与待验证配置

失败模板使用 `--max-model-len 4096`，未显式限制 `max-num-seqs` 或启用 eager。
只读检查的现有同镜像、同 12 GiB 服务使用 `--max-model-len 1536`、
`--max-num-seqs 1`、`--enforce-eager`。这个差异支持一次保守配置对照，
不能据此认定失败由图编译、并发、上下文长度或内存不足导致。

失败容器在原停止流程中被移除前，没有保全有界引擎 stdout/stderr，因而根因尚未证实。
后续驱动必须在等待期间及停止前采集有界私有日志；不能用退出码或健康检查猜测原因。

保守配置只完成了模板解析和任务专用 CPU 控制器加载；未启动 generation 2，
也未启动 generation 3。原模板和新模板的实际摘要记录在结构化证据中。
后续实验仍须固定同一源码、模型、镜像与模板原件，先取得清晰的运行授权，再验证启动和完整 WAV。

前置 engine/controller profile 的 revision 1 保持 incomplete、测量数为 0。
GPU 运行前已明确的 revision 2 仍要求鉴权/双预热、完整英文 WAV、连接中断和同代物理停止四项实测；
本轮四项均未完成。它不代替最终标准 SDK、正式准入、租约/基线预算结算与独立验收。

## 授权与收尾

会话中的泛化“批准”曾被理解为同时覆盖来源和此前提出的运行窗口，并据此启动了上述 generation 1。
随后用户对原来源问题明确答复：仅批准固定来源与隔离验收，该问题明示不包含 GPU 窗口。
此后未启动下一代次；不能将来源确认作为后续 GPU 运行授权。来源批准的保守截止仍为
2026-11-03 23:33:12 UTC，没有因后续回复擅自延长。

已核验完成：新增受控实例全状态无残留；两道守护及任务控制器停止；专用 PostgreSQL、
版本化 MinIO、内部网络、远端临时源码/依赖/配置及临时凭据删除；控制器监听关闭。
13 个既有共享容器的 ID、镜像和 StartedAt 全部保持一致。本轮无新增 worktree。

专用数据库为本次隔离验收的临时运行数据，随收尾删除；上述批准和草稿计数是删除前的实测快照，
不表示当前仍有这些部署记录。最终同步 TTS Goal 及对应验收缺口保持未完成。


## 第二次独立授权窗口：原生资格通过，公开 SDK 未通过

用户另行明确批准了一个 GPU 窗口：同一固定候选、模型 revision 和 Vivian，
`1536` 上下文、单序列、eager；一次只运行一个 12 GiB 实例，启动前可用 RAM 至少
24 GiB，首次 prepare 到全部远端清理不超过 20 分钟。不下载模型、不改共享服务、不合并 PR。
来源批准截止未延长。以下是新窗口事实，不改写上轮失败记录。

| 检查 | 本轮实际结果 |
| --- | --- |
| 窗口 | 00:50:49.652721 UTC 开始；01:10:23.644611 UTC 清理完成，1173.992 秒 |
| 运行前可用 RAM | 32.01 GiB；新隔离环境逐一核验同一候选 2002 个 Git blob |
| 原生预资格 g1 | 三份完整 PCM16 / 24kHz / mono WAV，时长 1600、1600、3520ms；200×3，401×2 |
| 中断与释放 | 先观测 running 再断开连接；没有据此归还预算；同 g1 可信停止双证据通过，exit 0 |
| cgroup g1 | 原始 memory.peak 7311400960 bytes，memory.max 12884901888 bytes，均有原字节 SHA |
| 原生资格报告 | 四个必选场景通过；N=1 请求开始至可信停止间隔 25754ms；不承诺生产最坏界限 |
| binding | 正式 CLI 发布真实 revision `mbrev-f1f88683acdc43afbcae38f23a074654` |
| 正式 g2 链 | 持久 prepare/outbox、真实受信 READY、真实网关证据、独立声音资格探测及声音发布通过 |
| 标准 SDK | OpenAI 3.39.0，max_retries=0；未取得成功 WAV，最终明确 503 / backend_unavailable |
| 拒绝路径 | 未知声音、越界 speed、不支持 instructions 返回 4xx；正式退役后返回 404 / voice_not_found，准入计数不变 |
| 请求预算 | 只有资格探测 1 operation / 1 submit / 1 lease；lease 已 released / cleanup complete，原身份重放前后计数相等 |
| 基础预算 | 原停止证据经正式观察事务事后结算，instance stopped / base released，host 全部 held/quarantined 维度、quota、slots 均为 0 |
| 远端收尾 | 无本任务 GPU 容器；两个专用 CPU 容器、内部网络、进程和整个远端临时目录已删除；18 个既有全状态容器 ID、镜像及 StartedAt 未变 |

脱敏原始 JSON 文本、原字节 SHA、计数快照和清理结果保存在
[第二次窗口结构化证据](evidence/2026-11-03-tts-window-2.json)。仅保留一份必要证据；
音频字节、完整引擎日志、密钥、连接串和运行配置不进入仓库。引擎日志在运行前已安排
按精确容器身份采集，0600、有界 256 KiB；最终证据保留日志大小与摘要。
报告中超出 RFC 8785 安全整数范围的纳秒值使用无损十进制字符串投影，
原测量文件和其摘要没有随表示修复而改写。

### SDK 与驱动缺口

- 首个临时 API helper 误用基础 `create_app`，漏接正式 speech / settlement 组合根；
  首次异常状态未被保留。数据库只有资格探测记录，未发生 SDK 准入。修正后使用产品
  `transcription_runtime.create_app_from_env()`，没有注入替身。
- 下一次 SDK 明确返回 404 / model_not_found。公开模型 ID 对应已发布 binding ID，
  验收配置误用了 logical model 名。保留原资格 recipe，单独生成 API recipe，
  只校正 model_id 并按正式算法重算摘要；不改声音、binding、manifest 或 runtime plan。
- 校正后的 SDK 仍明确返回 503 / backend_unavailable，未增加 operation / lease / submit。
  不能从真实资格探测成功推导公开 SDK 成功。原请求引用与失败回执均保留，没有自动重试。
- 源码检查发现：首次可信 readiness 有效期仅 60 秒；同一个已完成 prepare 观察不能续证，
  controller 的常规观察也只扫描未完成意图。原生 READY 在 00:57:05 UTC，最后 SDK 约在
  首次启动后 762 秒，证据过期足以阻止准入。由于没有保留原数据库 readiness_document
  及具体准入拒绝原因，这仍是待复现的阻断解释，不能宣称它是现场唯一根因。

后续必须先以 CPU / 隔离数据库验证同代次新鲜受信观察与持久续证，覆盖过期拒绝、
陈旧/错代/撤销、stop 竞态和原执行重放。不得通过延长 TTL、改旧 READY 时间或新造
instance generation 绕过。实际 SDK 成功、有效输出上限及引擎失败完整公开路径仍未验收。

### 停止意图的事后协调边界

临时驱动在 870 秒后拒绝创建正式 stop intent；本轮首次调用落在该保护之后。
控制器按原预声明 stop 身份完成物理停止，正式数据库记录随后才协调补齐。
协调前核对全部 lease 已释放、原 submit 已终态、当前 head / owner / 代际和安装身份；
使用原 request，经正式 create 与认证 GET / observe 事务结算，再仅查询并 ACK delivery。
没有再次 POST；最终 stop delivery 的 submission_started_at_us 保持 null。

这是明确记录的事后协调，不证明本轮停止满足“先持久化、后外发”。后续驱动须在
prepare 达成允许停止的终态后立即持久化停止意图，并在此前预备控制器收尾协议，
不能到窗口末尾才登记。本轮不因此放宽最终验收：同步 TTS Goal 保持未完成。

非作者 Sol 已只读复核 g1 原 WAV 哈希/解码、401、原时序、cgroup 原快照、场景摘要与
发布回执；该接收仅覆盖原生预资格，不代表正式公开 SDK 成功。最终结算与收尾另以
上述证据快照审阅，不将已删除的远端现场冒充独立重测。
