---
title: 受控 TTS 验收与资源收尾
status: incomplete
date: 2026-11-03
---

# 受控 TTS 验收与资源收尾

最新结论：第五次窗口已取得标准 SDK 的真实 HTTP 200 和可解码 WAV，停止、正式结算及
清理均完成。但关联验收失败，且脚本存在 JSON 排序回读后误判的确定缺陷；临时账本随后
被清理，未保全原有效命令
摘要与 staging PCM 一致性验证；完整 Goal 仍未接收。第四窗口确认的文本超资格问题已修复。
各次窗口事实按下文分别保留。

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

若要在原 readiness 到期后持续接单，必须先以 CPU / 隔离数据库验证同代次新鲜受信观察与持久续证，覆盖过期拒绝、
陈旧/错代/撤销、stop 竞态和原执行重放。不得通过延长 TTL、改旧 READY 时间或新造
instance generation 绕过。实际 SDK 成功、有效输出上限及引擎失败完整公开路径仍未验收。

### 停止意图的事后协调边界

临时驱动在 870 秒后拒绝创建正式 stop intent；本轮首次调用落在该保护之后。
控制器按原预声明 stop 身份完成物理停止，正式数据库记录随后才协调补齐。
协调前核对全部 lease 已释放、原 submit 已终态、当前 head / owner / 代际和安装身份；
使用原 request，经正式 create 与认证 GET / observe 事务结算，再仅查询并 ACK delivery。
没有再次 POST；最终 stop delivery 的 submission_started_at_us 保持 null。

这是明确记录的事后协调，不证明本轮停止满足“先持久化、后外发”。后续驱动须在
业务请求结束、prepare 已终态、lease 已释放且 submit 已终结后，先持久化停止意图再
外发停止；创建 stop 会进入 draining，不能在资格 probe / SDK 前提前登记。启动前
预备控制器退出协议，并为登记与结算保留明确时间，不能到窗口末尾才登记。本轮不因此放宽最终验收：同步 TTS Goal 保持未完成。

非作者 Sol 已只读复核 g1 原 WAV 哈希/解码、401、原时序、cgroup 原快照、场景摘要与
发布回执；该接收仅覆盖原生预资格，不代表正式公开 SDK 成功。最终结算与收尾另以
上述证据快照审阅，不将已删除的远端现场冒充独立重测。

## 第三次授权窗口：排除 readiness 过期，SDK 仍被拒绝

用户明确授权新窗口，并要求已授权、无新增风险的工作直接执行。生产源码仍固定
`90f53c4ead3236ccba61bc353259dc7f05e3a179`，标准 SDK 客户端来自
`ca280e8dba49a9b42c1eb08dc1c837b1a2bcd291`；原模型、镜像、Vivian、
12 GiB / 单实例 / 并发 1 及来源截止均未改变。运行前可用 RAM 32.27 GiB。

| 检查 | 本轮实际结果 |
| --- | --- |
| 原生预资格 g1 | 三份完整 PCM16 / mono / 24kHz WAV，1600、1600、3600ms；鉴权 401×2；同代停止双证据通过 |
| cgroup g1 | memory.peak 7082033152 bytes，memory.max 12884901888 bytes |
| 正式 g2 | 持久 prepare/outbox、真实 READY、网关证据、声音资格 probe 与发布通过；公开 model 使用真实 binding ID |
| 标准 SDK | 正式 `create_app_from_env()`，OpenAI 3.39.0，零重试；返回 503 / backend_unavailable，无成功 WAV |
| 拒绝时间 | SDK 结果文件时间位于数据库原 readiness 的 60 秒有效区间；本轮不能归因于过期 |
| 正式请求记录 | 始终只有 probe 的 1 operation / lease / submit；未出现 SDK 准入记录，内部拒绝原因尚未证明 |
| 拒绝与退役 | 未知声音、非法 speed、不支持 instructions 均为 4xx，保留具体 code；正式退役返回 404 / voice_not_found，计数不变 |
| 请求释放 | probe lease released / cleanup complete，正常结算；按原 lease 重放前后快照相等 |
| 正式停止 | 先持久化 stop，再标记 submission_started，之后外发原 stop；认证 GET / ACK 与正式观察完成，未走事后补建 |
| 最终预算 | instance stopped / base released；host 12 个 held/quarantined 维度、slots、quota 均为 0 |
| 远端收尾 | 服务、进程、CPU 容器与网络在 776.502 秒内清理；含整个临时目录在 868.396 秒内清理；18 个共享容器基线一致 |

单份脱敏快照、原字节 SHA、SDK 回执时间、原 readiness、两条正式 delivery 和最终计数见
[第三次窗口证据](evidence/2026-11-03-tts-window-3.json)。未保留临时数据库、连接串、
密钥、原生音频或备份目录。独立审计仅使用既有管理连接的强制 READ ONLY / REPEATABLE READ
事务；API、gateway、probe 和 settlement 的生产角色权限没有扩大。

停止和释放通过不改变 SDK 失败结论。后续先在 CPU / 隔离数据库复现正式公开准入，
记录内部拒绝码及 SQLSTATE，补足正式工厂与真实受限角色的成功路径覆盖；在原因明确和
必要修复通过后再安排真实引擎补验，不延长 TTL 或重复碰运气。同步 TTS Goal 保持未完成。

## 第四次独立授权窗口：定位验收文本超出资格

生产候选固定 `67f894331a8116dedbb402c2509b968e18f551d2`，SDK 工具固定
`ca280e8dba49a9b42c1eb08dc1c837b1a2bcd291`。模型 revision、Vivian、镜像、来源期限
和 12 GiB / 并发 1 / 最多两个顺序代次边界不变。本次授权已使用完毕。

| 检查 | 本轮实测 |
| --- | --- |
| 窗口 | 2026-11-03 04:52:27.836822 UTC 至 05:07:20.897126 UTC，893.060 秒 |
| 运行前 RAM | 32.188 GiB；每次 prepare 前再次检查至少 24 GiB |
| g1 原生资格 | 鉴权、双预热、完整英文 WAV、连接中断与同代可信停止通过；WAV 时长 1600 / 1600 / 3520ms |
| g2 正式链 | 持久 prepare/outbox、真实网关、可信 READY、声音资格探测与发布通过 |
| 标准 SDK | 仅一次请求，503 / backend_unavailable；私有诊断为 policy_or_resource_disabled，未建立 SDK operation / lease / submit |
| 有效期 | readiness 04:59:16.286411 UTC 核验、05:00:16.286411 UTC 到期；SDK 失败发生于有效期内 |
| 能力边界 | 批准与发布均为 complete / en / wav / 24000 Hz / max_input_codepoints=32；工具文本实际为 48 字符 |
| 离线诊断 | 同一生产 require_qualified_parameters 与实际能力：48 字符拒绝，27 字符文本通过；不写数据库、不再调用引擎 |
| 正式停止与结算 | 原 stop intent / 原身份 GET ACK / 正式观察完成；instance stopped、base released，host、quota、slots held/quarantined 全零 |
| 租约重放 | 原资格探测 lease released / cleanup complete；原身份重放前后计数不变，无重复释放 |
| 最终收尾 | 本任务进程、容器、网络与远端临时目录全部清除；18 个共享容器基线一致，无新增 worktree |

[第四次窗口结构化证据](evidence/2026-11-03-tts-window-4.json) 保留 30 份脱敏原始 JSON
的解析内容、原字节摘要及大小、日志摘要、helper 摘要和最终清理回执；不包含音频、完整日志或凭据。
文件 SHA-256 为 `af925a47a0e8ae16f0f2d5c0dd9f419d72ee49b34eedf56cfc3d6e3a3477345b`。
原始纳秒值保留原表示；报告编译使用无损字符串投影，不改测量事实。

本轮还修正了临时驱动的上下文字段与只读观察身份。观察身份使用专用数据库 owner 的
READ ONLY REPEATABLE READ 事务；没有扩大生产权限，实际租约重放仍使用原 settlement 角色。
原停止与正式结算在清理前完成，不以删除容器代替预算释放。

48 > 32 是已证实的充分拒绝条件；通用拒绝码并不能排除所有其他条件。
后续修复仅将验收工具文本改为 `A short English voice test.`（27 字符），不扩大资格、
不放宽生产校验。新增回归将真实标准 SDK 请求交给生产能力校验器，覆盖上述 32 字符边界。
这是 CPU 回归，不能计作真实 SDK 合成成功；下一次运行须固定修复后的工具版本，
同步 TTS Goal 仍未完成。

## 第五次独立授权窗口：真实 SDK 成功，完整关联证据仍缺失

生产继续固定 `67f894331a8116dedbb402c2509b968e18f551d2`，SDK 工具更新为
`fc0ed160a5c01fec980be6061a0ea77e814e8dd7`，使用 27 字符合成英文。
原模型 revision、Vivian、镜像、来源截止及资源限制不变；SDK 未重发。

| 检查 | 本轮实测 |
| --- | --- |
| 窗口（北京时间） | 2026-11-03 13:44:49.587718 至 13:59:14.184018，864.596 秒 |
| 运行前 RAM | 32.315 GiB；控制器 healthz / readyz 均 200，每次 prepare 另核验至少 24 GiB |
| 固定制品 | 2002 个 Git blob 一致；SDK 脚本 SHA-256 为 `393ad43988d986610fa417354ad1fd8b6df0140909e7c841ee0f7cce794e849c`；7 个锁定 SDK wheel 核验通过 |
| 原生 g1 | 鉴权、双预热、英文完整 WAV、连接中断及可信停止通过；WAV 1600 / 1600 / 3520ms |
| g2 正式公开链 | 正式 prepare/outbox、真实 READY / 网关、资格 probe 及声音发布后，唯一标准 OpenAI SDK 请求返回 200 |
| SDK WAV | 88364 bytes，PCM16 / 24000 Hz / mono，44160 frames、1840ms；完整解码且 PCM 非零，在十秒上限内 |
| WAV 摘要 | `sha256:72d659998540870cbf8cb77a594579d6d9fb647dda609b76faf6de2f1f028bf2` |
| 解码 PCM 摘要 | `sha256:c8b5cecb803255f1b943f09e4efefa52c75a1bb172b5679d7ca73e56a98d042a`；未与已删除的 staging 对象重新比对 |
| 准入与租约 | probe 和 SDK 各一组 operation / attempt / lease / submit；两条 lease 均 released / cleanup complete、settled |
| 重放与预算 | 两条原 lease 重放前后完全一致；instance stopped / base released，host 十二维、slots、quota held/quarantined 全零 |
| 正常停止 | stop-create → submission_started → 原 stop POST → 原意图 ACK → 正式 observe，全部通过 |
| 清理 | 专用服务于 595.954 秒完成清理；含远端目录、凭据及依赖于 864.596 秒全清，18 个共享容器基线一致，无新增 worktree |

[第五次窗口单份结构化证据](evidence/2026-11-03-tts-window-5.json) 保留 30 份脱敏 JSON
原字节摘要、解析内容、日志摘要、helper 摘要、音频元数据/摘要、运行级身份推导与最终清理回执。
不导出音频、凭据或完整引擎日志；其 `missing` 同时包含未生成的历史派生文件和确实缺失的
`sdk-association-after.json`，须逐项按用途判定，不能把采集成功当作验收通过。

### 本轮驱动与关联缺口

首次驱动在 g1 prepare 成功后，由于本次部署遗漏私有 gateway / API context 生成而停止；
当时无 native 测量、endpoint 制品或 SDK 请求。补齐上下文后，核实原驱动已终止、两道守护仍
活动，沿原 g1 继续；不重新 prepare g1，不扩大代次、SDK 次数或窗口期限。接续子进程组
超时会整体终止并确认退出，异常继续保留原身份协调，不以删除代替释放。

SDK 的真实 WAV 成功与后续验收失败须分别记录。关联工具把数据库行转换成
`tuple(row.values())`；before 经 `sort_keys=True` 保存后，attempt 行列顺序与 SQL 返回顺序
不同，导致集合差分误判。已在 CPU 上复现这个会在读取网关原回执和 staging 对象前触发的
确定缺陷；现场只保留了泛化失败，未保留细分堆栈，不能补记实际失败步骤已独立观测。
驱动仍继续停止、结算并删除了临时 PostgreSQL / MinIO，使原 business/request/command
摘要和 staging PCM 同值校验无法补证。这里存在验收及清理流程缺陷，不能改写成完整接收。

本轮证据可以按显式 `lease_id` 集合差分及 settlement 外键，在同一 installation 中重建
唯一新增 SDK 的 operation / attempt / lease / submit 及释放关联；结果见 `derived_sdk_execution`。
这是现存权威快照的具名推导，不是恢复出已丢失的有效命令或网关回执，不能用“计数为 2”
代替关联证明。SDK 成功、可信停止与预算释放已证实；原有效命令及结果的完整关联尚未证实。

后续先将关联工具纳入版本管理，按显式列名比较身份并覆盖 JSON 排序回读；关联未验完时
先保留临时账本和对象供窗口内只读核实，仍须按原期限停止并清理。不得自动重发 SDK、
自动追加 GPU 窗口，或通过放宽关联要求结束 Goal。
