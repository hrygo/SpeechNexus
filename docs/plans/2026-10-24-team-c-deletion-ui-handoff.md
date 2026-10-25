# 团队 C：工作台删除预检、确认与恢复页面

2026-10-24，base_sha=head_sha为 `9eea85987232eaebbcbd6768c46fe0a084a4d177`，分支 `codex/team-c-c00-versions`。沿用完整C00–C08目标，没有创建提交或推送。本单元实现、验证与唯一独立终审的一轮修复结果如下；累计包最终校验值和全部文件hash以 `.runtime/team-c/team-c-current.manifest.json` 为准。

## 已实现

- 正式工作台“删除与恢复”入口，以真实项目/稿件目录和当前版本为预检目标。只支持C现有的workbench project/transcript入口；同源生成client消费三个冻结操作，不新增公共协议、对象区或生产组合根。
- 普通删除展示阻断，级联展示完整依赖及固定plan revision/digest/有效期；目标加全部cascade依赖唯一化后逐项确认。retain/独立副本不进入删除集合，相互矛盾的计划不能确认。
- 提交前持久化原键/原body；未知结果只能原键重放。存储损坏、quota或外部改写会阻断提交，已访问页面跨功能保留会话内存，恢复后仍需显式提交。
- POST接受快照与GET当前状态分开；读取失败不冒充完成或丢失ack。当前成功结果校验完整删除集合及计划内保留副本，失败/阻断仅展示稳定错误码；内容使用React纯文本。
- “恢复”指恢复查询/原意图，不能撤销已清理内容。计划过期、范围变化或明确拒绝后需显式结束并重新预检，不能自动确认替代范围。

## 实测与证据

本单元证据目录为 `.runtime/team-c/deletion-ui-9ee/`。最终15项focused测试（含两类重放各四种存储故障）及最初失败记录均保留；Web五门禁通过，**1283=805 contracts+79 Admin+399 Workbench**。Python format538、ruff、mypy399源文件、7依赖边界通过。28源契约及生成漂移本轮重新验证通过。Python生产源码/机器契约未变，上轮3006项unit/contract证据仍有效，本轮未重复宣称重新运行该全套测试。

PostgreSQL18.6 socket-only私有探针 `--browser --coverage`：**296 passed /22.95s/94.42%**，末次cluster `.runtime/team-c/pg-sgdr5it7` 已独立核对pg_ctl退出3且无服务。前两次失败分别为新测试漏传app必需参数、Node stdin未关闭导致进程超时，保留red日志；未将浏览器中间result当作整体验收通过。

Chromium156.0.8078.4生产bundle→真实C HTTP→私有PG：删除页一个context/五写请求，普通阻断、级联两目标逐项确认、preview/accept两次实际提交后丢回执、外部记录损坏阻断重放、刷新原键恢复、quota跨功能保留、物理正文清理与独立项目的同文稿件仍可读、原接受快照与GET当前完成分开、360px/基础focus+一次Tab、出站0/意外异常0。会议页既有真实C HTTP/PG/fake LLM恢复回归也通过，证据按输出路径分别保存。

复现先 `pnpm build`，再运行 `uv run python scripts/dev-env/workbench-pg-probe.py --pg-bin /path/to/postgresql/bin --browser --coverage --output .runtime/team-c/deletion-recheck.json`。浏览器产物写入该output同目录的 `deletion-recheck-browser/`，不再覆盖旧会议交付证据。测试只用合成数据及一次性loopback/PG，未运行模型、设备或生产删除。

## 仍未验证/交接

测试中的独立项目稿件保留不等于saved_asset对象完整依赖图/A07恢复水位资格；retain依赖的页面语义有合成fixture测试，真实独立对象区/账本备份恢复仍需生产实现和联合验收。当前页面不提供完整删除历史列表，也不替代备份恢复工具。

生产组合根、owner迁移/grants与同版Meeting删除图/cleanup仍由A串行集成。未改变A/B owner代码、模型/预算/双库资格；真实设备/网络/Linux/GPU、跨浏览器和完整键盘/读屏仍待完成。此前六Minor保持延期，完整Goal与G0–G5保持未完成。

下一业务单元继续公开realtime状态机/消费者、会议列表/要点/固定版本导出、Artifact导入/下载、独立副本与C03，按依赖和各工作包逐段推进。

## 终审与修复

唯一fresh终审Critical0/Important1/Minor0：I1指出重放前未重新核验持久化屏障。两类pending的回归先2failed/13passed，再修复为15passed；覆盖损坏、合法并发改写、读取失败和quota四种故障组合，不额外POST、保留原意图/外部值，修复存储后仅显式同键重放。每次replay都先走journal CAS及保存，失败进入blocked/storage。

一轮修复后Web五门禁1283和PG296/22.95s/94.42%、两个生产bundle浏览器再次通过；没有二次委派审查。终审后仅三处源码/测试发生具名变化：session、其回归测试及真实browser probe；双语README状态同步和交接/计划由主代理复核。没有本轮新增Minor，此前六项延期未改。

审查未裁决的生产独立对象图/A07恢复、真实资格和完整辅助技术仍保留为待验；主代理补齐实际门禁、累计包和issue读回。测试突变未授权且不作为已完成声明。全部裁决与成本见本单元progress.md。
