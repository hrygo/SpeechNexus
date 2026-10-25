# C06 持久纪要任务与发布事务交接

核验日期：2026-10-24（Asia/Shanghai）。`base_sha=head_sha=a98b2bbfa2576eeee9f5bdf37300605428499d84`。本单元推进工作台内部持久任务，完整 C00–C08 目标保持 active，未创建 commit/push。用户随后授权发送生成依赖清单并要求更新 issue；已发送给团队 A chat，并更新/回读 C00–C08 九张 issue 的标签、子任务勾选、证据和待办。

## 已实现范围

- `MinutesTask` 固定 sealed 源 revision ID/摘要、服务端 profile 与 binding 摘要。binding 由 endpoint、资格引用和 profile 形成，不含凭据；模板/模型/端点变更属于新意图，重试不得切换服务。正文与 attempt token 从应用对象 repr 隐藏。
- 新建独立 minutes task 内容、immutable intent、attempt、创建键和重试键表。无正文 intent 固定原任务/源身份、依赖摘要、实际模型/模板和生成预算；不存源/模板正文，清理后保留执行与计量审计。创建键仅保存 hash/意图摘要，原键重放返回原任务；queued 才能领取，重复领取及进程重建后的 running 不自动外发。失败后仅显式重试，保留独立 attempt、已知用量或 unknown；不重新执行 ASR。
- 复用 C-owned 短事务 UOW，parent→transcript→task 锁序；调用模型期间无 runtime PG 连接。发布与源/当前 head/删除检查同事务，取消/源变化/删除不被迟到正文覆盖。已完成 attempt 的同回执重放也重新检查源屏障，不返回旧稿的成功正文。
- 创建/领取/完成发生实际 COMMIT 后丢失响应，均按原身份恢复；应用不会把提交错误改写成 LLM 错误、再次 finish 或重新生成。取消会中断本机 HTTP task，保留中断事实；删除期间轮询不可见也统一收尾，保留合法迟到完整响应/用量而不发布正文，不宣称远端计算停止或释放父预算。
- owner-only migration fragment 和显式 grant：runtime 无 DDL/DELETE，不能更新冻结 profile/源/意图；cleanup 只能读取请求完成事实。未接 A 的生产迁移/组合根。

## 删除、未知执行与资格边界

源拥有本单元的内部候选行。逻辑删除接受后所有正常查询隐藏；只有无 attempt 或全部取得正常完整响应的请求允许 FK CASCADE 清除 task 内容/profile 投影；无正文 intent、attempt、时间/用量/完成摘要与创建/重试键保留，防止旧键复活并遵守 ADR 0005。数据库 guard 拒绝静默抹掉未知请求，失败、取消或响应丢失的 attempt 目前会阻止物理清理，仍需具备资格的服务端停止/对账入口。没有通过手改标志、超时、TTL 或 HTTP 取消绕过 guard。

`completion_confirmed` 仅表示获得合规的正常完整 LLM 响应，不是设备停止、资源释放或混合负载资格。显式重试不授予实时物理容量承诺。默认 LLM 仍关闭；无真实模型、外部 LLM、GPU 或生产配置变更。

## 当前验证

- 隔离 Unix-socket-only PostgreSQL 18.6 探针262 passed、无 skip；包含新真实 PG 场景和既有回归/adapter 单元，不称259项全部真实数据库测试。总 statement+branch coverage93.80%，新 task PG92%、app95%、domain100%。原重放缺陷先观察 RED（返回旧 succeeded 正文），修复后 GREEN；最终提交异常从两次写入 RED 改为一次写入 GREEN。
- 独立检查本轮10座具名临时集群 `pg_ctl status=3` 且 postmaster.pid 不存在；最新报告 `stopped=true`。证据 `.runtime/team-c/c06-tasks-pg.json`、`c06-tasks-pg-terminal.json`、`c06-tasks-pg.log`。
- Python ruff/format402文件、mypy292源文件、7项依赖边界通过；机器契约28文件及生成漂移检查通过。修复前原样全库1541 passed、596 skipped、1 failed；修复后原样 unit/contract 为1525 passed/1 failed（34.60s，无skip），仍是下述独立夹具；不宣称当前原样树全绿。缺环境的集成 skip 不计通过，工作台 PG 由上述无skip独立探针覆盖。
- 唯一失败仍是 B/A 网关测试使用固定 ledger clock 与 wall clock 的已知夹具问题，详情和未应用的修正候选见[上一交接](2026-10-24-team-c-c06-llm-handoff.md#全仓回归的独立阻塞与候选)。不放宽生产时间校验，网关源保持原 SHA。已从 A chat 读到 PR #70 合入修正的消息，C 尚未拉取验证；本轮继续交付 a98 固定基线包，后续按远端 exact SHA 集成。
- 75个Web源文件逐字节沿用前183文件包；复用512测试/构建和相同产物浏览器证据。本单元没有纪要页面、真实设备或部署网络验证。
- 图谱未索引，具名源码和实际探针是证据；没有创建索引。完成一次独立 sol_worker 终审：Critical0 / Important2 / Minor0。删除期间漏记结果和计量事实级联删除均先复现 RED，再在同一修复轮中通过262项探针；不再分派复审。累计191文件已在干净a98基线预览apply-check、实际应用、逐文件字节和260处本地文档链接/锚点验证；真实Git index未改。最终SHA以相邻manifest为准。

最终191文件预览仅在临时组合副本中应用既有网关时钟候选后，unit/contract **1526 passed**（33.21s），实际import和所有交付Python字节均核对。最终包后续仅调整文档，Python逐文件hash须与该预览匹配；证据 `.runtime/team-c/c06-tasks-python-candidate.json`。这不等于已在权威工作区修复网关；C源未修改B文件。

## 接入与剩余工作

公开 workbench OpenAPI仍 `0.1.0.dev0`，没有改 canonical 契约/生成产物、锁文件或 deploy。新增对象为内部应用/领域端口，不冒充 MinutesCreateRequest/MinutesJob。公开 runtime roots 仍需 A 协调扩展，随后 C 接 Meeting→sealed transcript、公开纪要路由和页面、展示处理范围、显式保存/导出与完整派生删除依赖图。助手 turn/插话/稳定文本到 TTS、有声书及各自真实验收继续推进。

依赖清单已按用户授权发送到 chat「作为团队 A 持续推进工作」，并区分本地 a98 与 A 远端 PR #70。C issue #35–#43 已同步并回读：C00/C01/C02/C04/C05/C06/C08=in-progress，C03/C07=planned，全部open；已完成子项与未完成复合父项分开标记，不勾选 Gate。审计记录 `.runtime/team-c/2026-10-24-issue-sync.json`。

本单元不证明公开 MinutesJob、Meeting 页面、生产迁移、停止对账、独立保存制品、实际内容出站限制或同设备物理预算已完成，也不关闭任何 Gate。

[实施计划](2026-10-24-team-c-c06-tasks-plan.md) · [团队目标](2026-10-24-team-c-goal.md) · [ADR 0003](../decisions/0003-workbench-llm-network-boundary.md)
