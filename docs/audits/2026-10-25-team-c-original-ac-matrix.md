# 团队 C 原 41 条 AC：语义子条件矩阵

核对日期：2026-10-25。依据：[下一阶段 Goal](../plans/2026-10-25-team-c-next-phase-goal-and-acceptance.md)，version 0.2.0。当前实现与验证结论见[集成报告](2026-10-25-team-c-next-phase-integration.md)。

本矩阵保留 41 条原 AC 的完整原文，形成 278 个带稳定标识的语义子条件草案。原文是范围依据，子条件不能缩减原文含义；拆解仍需人工覆盖复核。[可追踪映射](2026-10-25-team-c-acceptance-map.json)保存逐条件状态、责任人、源码/消费者/测试锚点、摘要与证据维度。原始 issue 快照及115个无损初拆子句仍保留在忽略目录 `.runtime/team-c/next-phase/2026-10-25T155546+0800/ac-matrix.json`。

**所有 AC 和语义子条件均未判定通过。** 全部278项已有候选映射，未审计数为0；候选映射内已发现的缺实现、缺正式消费者及缺行为断言继续明确留空，T02尚未完成。旧源码/证据摘要不自动刷新，不升级证明等级。原issue已在PR #153合并后仅更新进展总结，全部AC及勾选保持不变。

历史映射覆盖 C00/AC1/S01、S02、S04，C01/AC2/S01–S07、C03/AC2/S01–S06、C07/AC1/S01–S11、C07/AC2/S01–S08、C07/AC5/S05–S06、C08/AC4/S07。已直接复核6个变更文件及22个子条件，登记的41处源码失效引用已解除；旧摘要、逐文件理由及影响条件保存在映射的mapping_reviews中。当前严格审计仍检出126个映射角色缺项及2份历史证据缺少附属清单摘要，保持失败；映射角色缺项数不等于独立业务缺陷数，也不意味着候选映射已通过验收。

本次复核明确：render-unit v2只显式接收模型绑定，词典/预设写入及清空均诚实拒绝；当前快照不等于角色/规划版本/指纹和生成历史的完整正式消费。最后26项已逐条件核对，当前44项事件及49项导出/副本单元通过；实际合成反例确认非空非法ZIP、零章节进度仍触发对象写入/完成调用，领域完成填满计数。现有空字节、单次PUT失败和固定诊断保护不结算完整格式、缺章、检查点/续作、真实producer或全局日志资格。日志局部修复另见PR #166，不改挂旧基线证据或升级整体资格。

C00/AC2–AC4当前映射补充：版本/CAS/原键恢复已有应用与正式HTTP/浏览器消费者；公开项目历史范围、目标schema/对象区/角色仍须按各子条件验收。导入边界及Token门禁通过只证明其实际保护范围；完整交互及目标表权限仍待核验。删除启动重放和对象阶段屏障已有生产接线及隔离服务测试，A07备份恢复水位/跨模块源删除仍须联验。导出创建、查询、取消与原键恢复已有消费，真实格式producer及冻结公开下载/Range链路尚未接通；这些C自有缺口不挂成等待上游整包的理由。当前新增本地验证为8项导入边界反例、39项Token反例及两个源码门禁，未结算任何完整原AC。

C01/AC1当前映射补充：诊断只读取内网LLM配置与工作台依赖配置投影，完整语音模型/实例/资格/预算展示仍为C待接入。合成端点、配置available及UI ready不能证明真实模型资格。两个浏览器诊断和三个配置投影原CI节点已按实际checkout提取，不是本轮重跑；尚未将任何条件标为通过。

C01任务中心当前映射补充：正式消费者具备原键取消、显式刷新/恢复及事件触发完整重读；Job.progress展示、retryJob与列表筛选尚未实现，对应行为测试缺项明确保留。SSE断线后显式重读的完整浏览器操作仍待补。三个相关CI探针是实际Chromium、合成传输，无持久speech后端；固定70ab43d的本地speech factory五个公开Job操作实测404。A04公开持久Job制品与C自有消费者缺口分别记录，不用协调元数据、dev场景或回环响应冒充持久任务联验。详细范围见集成报告。

C02原稿/校对/导出当前映射补充：语音来源sealed原稿与追加draft/历史有PG及合成源证据；手工稿真实C浏览器校对不等于ASR资格。正式ASR→稿件桥接、固定校对revision下载消费和合法时间轴字幕导出仍有C自有缺口，未填入不存在的消费者/测试。原始JSON下载或私有文本helper均不结算正式文本/字幕闭环。

C02其余23项已补充，全部29个条件现在都有状态与边界说明。TTS请求固定原输入与voice/profile，但有效版本响应核验存在已复现失败（缺有效voice/profile仍确认），模型修订消费尚缺；WAV浏览器正例仅为合成，Range/MP3/raw PCM、通用断线恢复及目标凭据边界继续有空项或资格缺口。六个原CI探针按各自dev/回环/built-class/离线音频范围单列，未以浏览器或fake单执行计数结算真实TTS和不重复发布。

C03全20个条件现已核对：新增14项、复核历史6项。设计/克隆输入、材料重新确认已有合成浏览器保护；候选/再生成/试听/复用校验/发布与生命周期正式消费者仍待接。新负例实测目录适配拒绝公开合法revision并接受旧形状错身份/版本，现有旧夹具回归通过不掩盖该失败。固定手填TTS引用不等于已发布声音真实复用；无发布按钮不证明preview_only发布屏障。所有条件仍未验收。

C04/AC1–AC2新增16项：credit/gap/epoch/水位/文本冻结/连续PCM、采集与播放缓冲、旧generation及设备释放已建立局部映射；真实流、累计输出硬上界、物理设备和长时资格仍待验。新负例实测播放Context.close拒绝后仍报stopped，作为C自有清理确认缺口登记failed。当前126项unit/DOM通过不覆盖此失败；3个精确原CI浏览器节点各保留合成回环或fake-device/autoplay绕过范围，Worklet消费数不结算模型或扬声器资格。

C04/AC3–AC5另补20项，C04全36项均已映射但未验收。设备切换/系统音频正式入口与批准支持矩阵、真实权限撤销/系统休眠/断网、远端HTTPS/WSS、实际读屏和长时资格仍有缺口。现有真实C浏览器仅证明无会话不请求麦克风及初始键盘/焦点/360px，不结算完整实时可访问性。ASR历史Minor本轮实测send=0却发布sent2400，已登记failed并将相关水位条件改为C待修；TTS done后缓冲单独停止也仍待修复。

C05全部23项已有映射，尚未验收。保存稿件、追加封存版本和原键恢复有局部证据；实时session/gap/preview-final-aux联合来源、独立要点与正式固定版本会议导出仍缺接线。提词正式页为手动本地稿，服务端位置元数据不等于正式稿件绑定或真实语音跟随。10个精确原CI节点按五类保留实际checkout及合成边界，未重跑；当前65项Python和64项TS/DOM回归不结算真实模型/物理设备。普通会议/提词独立于LLM，完全禁用LLM启动后的新建/封存完整运行断言仍待补。

C06/AC1–AC2新增13项：默认关闭在助手store前拒绝，原HTTP/PG保护零turn/回执；固定端点/单次请求/无重定向/环境代理与配置绑定保护不替代部署无公网转发资格。纪要固定sealed源、完整profile/模板和绑定摘要，未知提交按原身份恢复、不自动重派running；llm_model字符串未证明不可变模型制品版本。12个原CI节点按实际checkout及fake边界提取，当前174项Python和14项TS/DOM通过；真实模型任务/完整lifespan/正式浏览器三项qualification未运行，所有条件仍未验收。

C06/AC3–AC5另补22项，C06全35项均已映射但未验收。助手正式执行、播放/插话/generation、独立保存/导出、远端停止释放/父预算仍缺接线与资格；running及有Job的failed/cancelled投影违反冻结schema，合法进展态原键回执被客户端拒绝，两组实际离线负例分别登记failed。PG回执/本地取消和owner注入失败、禁用态浏览器不替代启用态真实执行。

C08新增42项、复核原有日志1项，全43项已有状态与边界。删除预检/账本/对象完成/启动与稳态重放/无清理身份503有局部真实服务证据；独立备份水位、生产角色/迁移/镜像和目标拓扑仍待联验。初始页axe/跳转焦点与有限键盘/窄屏探针不等于全业务状态/物理设备/读屏支持矩阵。实际minutes两处及deletion sweeper仍泄漏合成异常内容，export安全对照不能外推；当前主干docs索引的旧能力说明、完整交接和旧Minor/抖动逐项收口仍待C推进。

## 映射与资格守卫

[检查器](../../scripts/dev-env/check-acceptance-map.py)独立冻结原41条AC全文及278个稳定子条件，逐项核对映射文件、源码摘要/锚点和证据标识；不能同时删改本文件与映射来缩小范围。证据维度分别声明，合成浏览器不会因“更高等级”推定真实模型、物理设备或目标拓扑。声明通过还需明确资格维度、完整head、对应运行的源码清单及其摘要、通过证据和语义复核记录；源码清单须覆盖本条件引用的源码/消费者/测试并匹配当前字节，不能仅更新映射摘要后复用旧运行。声明的metadata/source_manifest也须存在且摘要一致。失败、跳过、未知或旧head不能结算当前条件。

校验器只审计，不修改输入、不勾选AC、不放行Gate。报告不得覆盖矩阵、源范围、映射源码或证据，即使输入部分无效、路径使用symlink/hardlink别名也一样。原范围pin变更须独立复核并保留完整AC语义，不能用于掩盖缺项。

```bash
make acceptance-map-check ACCEPTANCE_EVIDENCE_ROOT=.runtime/team-c/next-phase/2026-10-25T155546+0800
```

需要逐项报告时运行：

```bash
uv run python scripts/dev-env/check-acceptance-map.py \
  --evidence-root .runtime/team-c/next-phase/2026-10-25T155546+0800 \
  --output .runtime/team-c/next-phase/2026-10-25T155546+0800/acceptance-map-audit.json
```

当前命令预期退出1，原因是尚未完成的映射及历史证据附属清单缺少摘要；严格审计不能为取得绿色而改为忽略缺项。映射检查通过也不代表验收通过，最终仍须逐语义条件核对实际断言。此入口尚未成为CI中的完整验收放行门禁；T02仍未完成。

## 预设草稿切片的有限证据

2026-10-25 17:06 核对：本表更新刚收敛的草稿切片，保留原 AC 全范围。Admin 145 项测试包含 27 项新增草稿测试；这些使用 fetch mock、内存 Storage 和 jsdom，不是实际浏览器、生产持久化或真实资格证据。复用 17:01 的已核验结果，本次只更新文档和映射。

| 子条件 | 当前实现/正式消费者及断言 | 未满足的范围 |
| --- | --- | --- |
| C00/AC1/S01、S02、S04 | [ProfileClient](../../web/admin/src/profiles/profile-client.ts) 消费同源生成请求/响应校验；[客户端测试](../../web/admin/src/profiles/profile-client.test.ts) 比较完整请求、大十进制及错身份/修订/定义响应；[正式装配](../../web/admin/src/App.tsx)挂入草稿页面 | 只覆盖草稿请求/响应和不注入默认值，未审计整个计划操作集合；不涉及事件覆盖 |
| C01/AC2/S01 | [草稿页面](../../web/admin/src/profiles/ProfileHost.tsx)显式选择新系列/null 或已知分配修订；[DOM 测试](../../web/admin/src/profiles/ProfileHost.test.tsx)断言 CAS 冲突保留定义、不自动重基 | 真实服务并发 CAS、其他管理写入及发布/激活 CAS 尚未联验 |
| C01/AC2/S02 | [ProfileSession.prepare/confirm](../../web/admin/src/profiles/profile-session.ts)分开准备和发送；DOM 断言核对不外发、确认才提交并聚焦确认按钮 | 实际浏览器操作、真实服务记录及其余管理写入待验证 |
| C01/AC2/S06 | [ProfileJournal](../../web/admin/src/profiles/profile-journal.ts)保留完整原意图；[会话测试](../../web/admin/src/profiles/profile-session.test.ts)断言未知/读取/原键重放及稳定冲突，[存储测试](../../web/admin/src/profiles/profile-journal.test.ts)断言原键和大修订持久于假存储 | 实际刷新/sessionStorage 与生产原结果恢复未验证；固定 GET 不能结算写入 |
| C01/AC2/S07 | 正式草稿页面与 Admin 外壳说明无身份隔离；[外壳测试](../../web/admin/src/App.test.tsx)和 DOM 测试保护相应文案 | 不证明入口网关限制或其他页面/部署边界已经全部核验 |
| C01/AC2/S03–S05 | [生命周期设计](../architecture/profile-lifecycle-contract.md)明确未来契约与私有准备边界，当前产品只注册草稿创建/固定读取；页面明确生命周期未接入 | A02 仍需交付可信资格生产者、发布/激活事务及持久原结果、当前指针和正式路由；退役需冻结行为并交付生产路径。C 取得具名制品后接入并联验；阻塞这些子条件，不阻塞草稿独立联调 |

上述 10 个子条件更新了源码/消费者/测试或依赖边界，所有验收状态仍为 `not_run`。源码及证据哈希存于同一忽略证据目录；没有用局部草稿测试结算任何完整原 AC。

## C00

### C00/AC1

来源：[issue #35](https://github.com/hrygo/SpeechNexus/issues/35)，正文更新时间 `2026-10-25T07:35:44Z`；原文 SHA-256 `c3ca0f75c3f3b1f644f3eb0b97756c0fda4a191b5a9f3863281c1870b4cbc092`。

> 计划范围的请求/响应/事件由同源生成入口消费，wire 默认值不另行维护，开发 fake 不注入真实遥测或标 ready。

- `C00/AC1/S01`：计划范围请求由同源生成入口消费。
- `C00/AC1/S02`：计划范围响应由同源生成入口消费。
- `C00/AC1/S03`：计划范围事件由同源生成入口消费。
- `C00/AC1/S04`：wire 默认值不另行维护。
- `C00/AC1/S05`：开发 fake 不注入真实遥测。
- `C00/AC1/S06`：开发 fake 不标 ready。

### C00/AC2

来源：[issue #35](https://github.com/hrygo/SpeechNexus/issues/35)，正文更新时间 `2026-10-25T07:35:44Z`；原文 SHA-256 `c379c888ed2bcae79a1168db1568ef3e1057c34bf5379c5933769949df57f95e`。

> 项目/稿件 revision 不可变，并发 CAS、原键恢复与历史读取一致；独立 schema/对象区及最小运行角色在真实部署装配可用。

- `C00/AC2/S01`：项目 revision 不可变。
- `C00/AC2/S02`：稿件 revision 不可变。
- `C00/AC2/S03`：并发 CAS 拒绝冲突且保持历史。
- `C00/AC2/S04`：结果未知沿原键恢复。
- `C00/AC2/S05`：历史读取与固定 revision 一致。
- `C00/AC2/S06`：独立 schema 在真实部署装配可用。
- `C00/AC2/S07`：独立对象区在真实部署装配可用。
- `C00/AC2/S08`：最小运行角色在真实部署装配可用。

### C00/AC3

来源：[issue #35](https://github.com/hrygo/SpeechNexus/issues/35)，正文更新时间 `2026-10-25T07:35:44Z`；原文 SHA-256 `cedd7c784f84e5874443d4810929e28f80adf69af6c9663aaa40acc127d3c2f1`。

> 工作台不导入 speech 内部或共写底座表，共享 Token/交互语义保持一致。

- `C00/AC3/S01`：工作台不导入 speech 内部。
- `C00/AC3/S02`：工作台不共写底座表。
- `C00/AC3/S03`：共享 Token 保持相同语义。
- `C00/AC3/S04`：共享交互语义保持一致。

### C00/AC4

来源：[issue #35](https://github.com/hrygo/SpeechNexus/issues/35)，正文更新时间 `2026-10-25T07:35:44Z`；原文 SHA-256 `22f55a19b5916e1f83deb897ef09c7d2da943a9396fff33d38c73431335dd01f`。

> 与 A07 对齐独立副本/对象删除及恢复水位；恢复后才开放相应读取，公开导出格式和消费者按冻结契约工作。

- `C00/AC4/S01`：与 A07 对齐独立副本删除边界。
- `C00/AC4/S02`：与 A07 对齐对象删除和恢复水位。
- `C00/AC4/S03`：恢复后才开放相应读取。
- `C00/AC4/S04`：公开导出格式按冻结契约工作。
- `C00/AC4/S05`：正式导出消费者按冻结契约工作。

## C01

### C01/AC1

来源：[issue #36](https://github.com/hrygo/SpeechNexus/issues/36)，正文更新时间 `2026-10-25T07:35:48Z`；原文 SHA-256 `78059a64e85ba0ea8c63a4f7b7de4a31eee4d49ccb84e8bf3e7bdba2dd22d726`。

> 界面展示实际模型/资格/就绪及资源预算事实，能力仅使用 ready/contract/planned，不以 fake 填充真实状态。

- `C01/AC1/S01`：界面展示实际模型事实。
- `C01/AC1/S02`：界面展示实际资格和就绪事实。
- `C01/AC1/S03`：界面展示实际资源预算事实。
- `C01/AC1/S04`：能力仅使用 ready/contract/planned。
- `C01/AC1/S05`：不以 fake 填充真实状态。

### C01/AC2

来源：[issue #36](https://github.com/hrygo/SpeechNexus/issues/36)，正文更新时间 `2026-10-25T07:35:48Z`；原文 SHA-256 `bef39bf5abc107008226619cc79096f2087c4d60621f2bfbffdbd89d4bd09068`。

> 管理写入遵守版本 CAS 和明确确认；预设发布/回滚/退役及冲突有可操作结果，不伪装用户权限边界。

- `C01/AC2/S01`：管理写入遵守版本 CAS。
- `C01/AC2/S02`：管理写入经过明确确认。
- `C01/AC2/S03`：预设发布有可操作结果。
- `C01/AC2/S04`：预设回滚有可操作结果。
- `C01/AC2/S05`：预设退役有可操作结果。
- `C01/AC2/S06`：管理冲突有可操作恢复结果。
- `C01/AC2/S07`：管理入口不伪装用户权限边界。

### C01/AC3

来源：[issue #36](https://github.com/hrygo/SpeechNexus/issues/36)，正文更新时间 `2026-10-25T07:35:48Z`；原文 SHA-256 `8e6416187681faa8160698f6b73961a6003056c6b5b4077737d8874447bc2b0f`。

> 任务详情/进度/取消/重试与持久后端一致，刷新、SSE 断线或历史过期后重读权威状态，未知不显示成功。

- `C01/AC3/S01`：任务详情与持久后端一致。
- `C01/AC3/S02`：任务进度与持久后端一致。
- `C01/AC3/S03`：任务取消与持久后端一致。
- `C01/AC3/S04`：任务重试与持久后端一致。
- `C01/AC3/S05`：刷新后重读权威状态。
- `C01/AC3/S06`：SSE 断线后重读权威状态。
- `C01/AC3/S07`：事件历史过期后重读权威状态。
- `C01/AC3/S08`：未知不显示成功。

### C01/AC4

来源：[issue #36](https://github.com/hrygo/SpeechNexus/issues/36)，正文更新时间 `2026-10-25T07:35:48Z`；原文 SHA-256 `c64592b077e5fd87b00a7061ec3c5ce4cdf1d7cf332c0253e628aa45c35337f4`。

> 列表分页/筛选及恢复有真实浏览器证据，诊断/观测有界且不包含业务内容。

- `C01/AC4/S01`：列表分页有真实浏览器证据。
- `C01/AC4/S02`：列表筛选有真实浏览器证据。
- `C01/AC4/S03`：列表恢复有真实浏览器证据。
- `C01/AC4/S04`：诊断与观测有界。
- `C01/AC4/S05`：诊断与观测不包含业务内容。

## C02

### C02/AC1

来源：[issue #37](https://github.com/hrygo/SpeechNexus/issues/37)，正文更新时间 `2026-10-25T07:35:40Z`；原文 SHA-256 `41e6fe1679e3b7183dd6d41219db66fdd54566c77487bd6141fdb689e2f76487`。

> 原始转写与追加校对 revision 分层，原稿/历史保留；真实 ASR 结果可导入、校对并按固定版本导出文本/字幕。

- `C02/AC1/S01`：原始转写与追加校对 revision 分层。
- `C02/AC1/S02`：原稿和历史保留。
- `C02/AC1/S03`：真实 ASR 结果可导入。
- `C02/AC1/S04`：真实 ASR 结果可校对。
- `C02/AC1/S05`：按固定版本导出文本。
- `C02/AC1/S06`：按固定版本导出字幕。

### C02/AC2

来源：[issue #37](https://github.com/hrygo/SpeechNexus/issues/37)，正文更新时间 `2026-10-25T07:35:40Z`；原文 SHA-256 `327386a2bc25ad73856cacf2e680a56e17d905b22f5436936f6f66cf1200ff80`。

> TTS 固定文本、voice/profile/model revision，真实结果可试听/下载，重试不重复发布。

- `C02/AC2/S01`：TTS 固定文本。
- `C02/AC2/S02`：TTS 固定 voice revision。
- `C02/AC2/S03`：TTS 固定 profile revision。
- `C02/AC2/S04`：TTS 固定 model revision。
- `C02/AC2/S05`：真实 TTS 结果可试听。
- `C02/AC2/S06`：真实 TTS 结果可下载。
- `C02/AC2/S07`：TTS 重试不重复发布。

### C02/AC3

来源：[issue #37](https://github.com/hrygo/SpeechNexus/issues/37)，正文更新时间 `2026-10-25T07:35:40Z`；原文 SHA-256 `405ff1b654e8df8658c68e1c23a03455114668df2d3a128ebc18c21a33c40447`。

> 同键重放、取消未知、冲突、刷新/断线和事件过期保持原身份恢复，迟到结果不覆盖新上下文。

- `C02/AC3/S01`：同键重放保持原身份。
- `C02/AC3/S02`：取消未知保持原身份恢复。
- `C02/AC3/S03`：冲突保持原身份恢复。
- `C02/AC3/S04`：刷新保持原身份恢复。
- `C02/AC3/S05`：断线保持原身份恢复。
- `C02/AC3/S06`：事件过期保持原身份恢复。
- `C02/AC3/S07`：迟到结果不覆盖新上下文。

### C02/AC4

来源：[issue #37](https://github.com/hrygo/SpeechNexus/issues/37)，正文更新时间 `2026-10-25T07:35:40Z`；原文 SHA-256 `d2e8bbb5a98e5e7c5ec7edc9f73061bf9aa0f85bdc14ca43dac2fa249ea94622`。

> Range、MP3/WAV/PCM 及 Asset/Artifact 过期边界符合契约，浏览器不持有引擎/数据库/组件凭据。

- `C02/AC4/S01`：Range 边界符合契约。
- `C02/AC4/S02`：MP3 格式边界符合契约。
- `C02/AC4/S03`：WAV 格式边界符合契约。
- `C02/AC4/S04`：PCM 格式边界符合契约。
- `C02/AC4/S05`：Asset 过期边界符合契约。
- `C02/AC4/S06`：Artifact 过期边界符合契约。
- `C02/AC4/S07`：浏览器不持有引擎凭据。
- `C02/AC4/S08`：浏览器不持有数据库凭据。
- `C02/AC4/S09`：浏览器不持有组件凭据。

## C03

### C03/AC1

来源：[issue #38](https://github.com/hrygo/SpeechNexus/issues/38)，正文更新时间 `2026-10-25T07:36:06Z`；原文 SHA-256 `c4b5e1d99ea279fbb6e15471fadff156c8dc9fdde4c484f74e654cfcbda8af40`。

> 设计/克隆输入与材料确认分别处理，材料改变后必须重新确认，真实声音模式分别有资格证据。

- `C03/AC1/S01`：设计输入和材料确认独立处理。
- `C03/AC1/S02`：克隆输入和材料确认独立处理。
- `C03/AC1/S03`：材料改变后必须重新确认。
- `C03/AC1/S04`：真实设计模式有资格证据。
- `C03/AC1/S05`：真实克隆模式有资格证据。

### C03/AC2

来源：[issue #38](https://github.com/hrygo/SpeechNexus/issues/38)，正文更新时间 `2026-10-25T07:36:06Z`；原文 SHA-256 `92b9bc54356107d2f5e2d72e5b346d6969e7d84bd8788b69e3ac52453ec9317d`。

> 候选、再生成、试听和复用校验状态可观察，失败原因明确；preview_only 不能发布。

- `C03/AC2/S01`：候选状态可观察。
- `C03/AC2/S02`：再生成状态可观察。
- `C03/AC2/S03`：试听状态可观察。
- `C03/AC2/S04`：复用校验状态可观察。
- `C03/AC2/S05`：失败原因明确。
- `C03/AC2/S06`：preview_only 不能发布。

### C03/AC3

来源：[issue #38](https://github.com/hrygo/SpeechNexus/issues/38)，正文更新时间 `2026-10-25T07:36:06Z`；原文 SHA-256 `0fb4bea769d9a44c04be50200d4deda1f2f0dc6c2a92cae4cba519109f40f215`。

> 只有复用资格通过并显式确认才发布不可变 VoiceRevision，后续真实 TTS 使用该已发布引用。

- `C03/AC3/S01`：仅复用资格通过的声音可以发布。
- `C03/AC3/S02`：发布前显式确认。
- `C03/AC3/S03`：发布的 VoiceRevision 不可变。
- `C03/AC3/S04`：后续真实 TTS 使用已发布引用。

### C03/AC4

来源：[issue #38](https://github.com/hrygo/SpeechNexus/issues/38)，正文更新时间 `2026-10-25T07:36:06Z`；原文 SHA-256 `d6d1872a9c9ccc278996e4df087b3fed3c335b874c1a27bd85959e0db967c5a1`。

> 退役/禁用/删除结果及共享部署文案与实际生命周期一致，合成页面证据不作为真实复用资格。

- `C03/AC4/S01`：退役结果与实际生命周期一致。
- `C03/AC4/S02`：禁用结果与实际生命周期一致。
- `C03/AC4/S03`：删除结果与实际生命周期一致。
- `C03/AC4/S04`：共享部署文案与生命周期一致。
- `C03/AC4/S05`：合成页面证据不作为真实复用资格。

## C04

### C04/AC1

来源：[issue #39](https://github.com/hrygo/SpeechNexus/issues/39)，正文更新时间 `2026-10-25T07:36:04Z`；原文 SHA-256 `1b6871aa82411f52cb9a12697876a880679a1c55c08ff1fdcfddf6f94f46f2e7`。

> 真实双向流中 credit/gap/epoch、水位、文本冻结与连续 PCM 一致，采集/播放缓冲和输出上界有界，PCM 不进入 React 查询缓存。

- `C04/AC1/S01`：真实双向流 credit 一致。
- `C04/AC1/S02`：真实双向流 gap 一致。
- `C04/AC1/S03`：真实双向流 epoch 一致。
- `C04/AC1/S04`：真实双向流水位一致。
- `C04/AC1/S05`：真实双向流文本冻结一致。
- `C04/AC1/S06`：真实双向流连续 PCM 一致。
- `C04/AC1/S07`：采集缓冲有界。
- `C04/AC1/S08`：播放缓冲有界。
- `C04/AC1/S09`：输出上界有界。
- `C04/AC1/S10`：PCM 不进入 React 查询缓存。

### C04/AC2

来源：[issue #39](https://github.com/hrygo/SpeechNexus/issues/39)，正文更新时间 `2026-10-25T07:36:04Z`；原文 SHA-256 `47e4bce05eb5c2c34afed9f7018a30d1feeb36dd4b91be3970675f8eb6b62cd2`。

> 旧 generation/迟到音频不串播，停止、离开、异常或迟到授权后本地设备/AudioContext 均正确释放。

- `C04/AC2/S01`：旧 generation 不串播。
- `C04/AC2/S02`：迟到音频不串播。
- `C04/AC2/S03`：停止后释放本地设备和 AudioContext。
- `C04/AC2/S04`：离开后释放本地设备和 AudioContext。
- `C04/AC2/S05`：异常后释放本地设备和 AudioContext。
- `C04/AC2/S06`：迟到授权后释放过时设备和 AudioContext。

### C04/AC3

来源：[issue #39](https://github.com/hrygo/SpeechNexus/issues/39)，正文更新时间 `2026-10-25T07:36:04Z`；原文 SHA-256 `0891b87feff75230e68f0b4f6f48e46a637c98f4a2ce76ef1f12b8517433ed6d`。

> 支持浏览器下权限拒绝/撤销、设备切换、后台/休眠及断网重连均有实际结果，无法无缝恢复时明确 gap。

- `C04/AC3/S01`：支持浏览器权限拒绝有实际结果。
- `C04/AC3/S02`：支持浏览器权限撤销有实际结果。
- `C04/AC3/S03`：支持浏览器设备切换有实际结果。
- `C04/AC3/S04`：支持浏览器后台与休眠有实际结果。
- `C04/AC3/S05`：支持浏览器断网重连有实际结果。
- `C04/AC3/S06`：无法无缝恢复时明确 gap。

### C04/AC4

来源：[issue #39](https://github.com/hrygo/SpeechNexus/issues/39)，正文更新时间 `2026-10-25T07:36:04Z`；原文 SHA-256 `6fcaee36b8ad7ccf4f8543b35021fbf8ed307982795acb4c78e60651523833db`。

> 真实远端通过批准 HTTPS/WSS 同源入口工作；麦克风/系统音频按支持矩阵开放，合成、物理设备及 GPU 证据分别记录。

- `C04/AC4/S01`：真实远端使用批准的同源 HTTPS 入口。
- `C04/AC4/S02`：真实远端使用批准的同源 WSS 入口。
- `C04/AC4/S03`：麦克风按支持矩阵开放。
- `C04/AC4/S04`：系统音频按支持矩阵开放。
- `C04/AC4/S05`：合成证据单独记录。
- `C04/AC4/S06`：物理设备证据单独记录。
- `C04/AC4/S07`：GPU 证据单独记录。

### C04/AC5

来源：[issue #39](https://github.com/hrygo/SpeechNexus/issues/39)，正文更新时间 `2026-10-25T07:36:04Z`；原文 SHA-256 `2835689a07be606e1a03665df1cd5c1dac03bb4ebb926eb57a046bf8e797e3d4`。

> 完整键盘/焦点/小窗口/读屏和长时媒体行为可验证；已知 Minor 有明确处理结果，不能由合成样本播放数代替完整资格。

- `C04/AC5/S01`：完整键盘行为可验证。
- `C04/AC5/S02`：完整焦点行为可验证。
- `C04/AC5/S03`：小窗口行为可验证。
- `C04/AC5/S04`：读屏行为可验证。
- `C04/AC5/S05`：长时媒体行为可验证。
- `C04/AC5/S06`：已知 Minor 有明确处理结果。
- `C04/AC5/S07`：合成样本播放数不替代完整资格。

## C05

### C05/AC1

来源：[issue #40](https://github.com/hrygo/SpeechNexus/issues/40)，正文更新时间 `2026-10-25T07:35:58Z`；原文 SHA-256 `9f1d0183a442ceb65d0c885900eef7b257c528eadf653db2e8ccb3bf7e6929e2`。

> 会议保留 session/转写来源与缺口，preview/final/辅助 revision 匹配；封存原文不被后续校对改写。

- `C05/AC1/S01`：会议保留 session 来源。
- `C05/AC1/S02`：会议保留转写来源与缺口。
- `C05/AC1/S03`：preview revision 匹配。
- `C05/AC1/S04`：final revision 匹配。
- `C05/AC1/S05`：辅助 revision 匹配。
- `C05/AC1/S06`：后续校对不改写封存原文。

### C05/AC2

来源：[issue #40](https://github.com/hrygo/SpeechNexus/issues/40)，正文更新时间 `2026-10-25T07:35:58Z`；原文 SHA-256 `76f2a37cca605ad9933ade6696d949ffdb8f10ed8074e0a9de2b430008b14648`。

> 要点可关联相应会话/转写版本保存，会议按固定版本封存/导出；未保存音频不承诺会后重新识别。

- `C05/AC2/S01`：要点关联相应会话版本保存。
- `C05/AC2/S02`：要点关联相应转写版本保存。
- `C05/AC2/S03`：会议按固定版本封存。
- `C05/AC2/S04`：会议按固定版本导出。
- `C05/AC2/S05`：未保存音频不承诺会后重新识别。

### C05/AC3

来源：[issue #40](https://github.com/hrygo/SpeechNexus/issues/40)，正文更新时间 `2026-10-25T07:35:58Z`；原文 SHA-256 `0369bd092874162599d3aed65b4d2b51effabb63fb9f461ab330575ff6efd39f`。

> 提词固定稿件 revision、有限跟随窗口，低置信时暂缓，旧稿/旧 epoch 不推动位置。

- `C05/AC3/S01`：提词固定稿件 revision。
- `C05/AC3/S02`：跟随窗口有界。
- `C05/AC3/S03`：低置信时暂缓。
- `C05/AC3/S04`：旧稿不推动位置。
- `C05/AC3/S05`：旧 epoch 不推动位置。

### C05/AC4

来源：[issue #40](https://github.com/hrygo/SpeechNexus/issues/40)，正文更新时间 `2026-10-25T07:35:58Z`；原文 SHA-256 `ef7d65b8d4ccce6c68e9184ef01ee55c6db97201761e77eb931e00f0f0f0f25b`。

> 真实语音跟随可被键盘/手动接管，普通会议和提词不依赖 LLM；实时来源/分人/对齐消费者有联合证据。

- `C05/AC4/S01`：真实语音跟随可被键盘接管。
- `C05/AC4/S02`：真实语音跟随可被手动接管。
- `C05/AC4/S03`：普通会议不依赖 LLM。
- `C05/AC4/S04`：普通提词不依赖 LLM。
- `C05/AC4/S05`：实时来源消费者有联合证据。
- `C05/AC4/S06`：分人消费者有联合证据。
- `C05/AC4/S07`：对齐消费者有联合证据。

## C06

### C06/AC1

来源：[issue #41](https://github.com/hrygo/SpeechNexus/issues/41)，正文更新时间 `2026-10-25T07:36:02Z`；原文 SHA-256 `25f081c86f72d13ec1f2bf3a1f2429c0b1b3235675d72e56e576bf8d03d206a2`。

> LLM 默认关闭，仅允许服务端批准本机/内网端点且无公有云回退；关闭时在事务前拒绝，不留下已受理 turn/回执。

- `C06/AC1/S01`：LLM 默认关闭。
- `C06/AC1/S02`：LLM 仅允许服务端批准本机或内网端点。
- `C06/AC1/S03`：LLM 无公有云回退。
- `C06/AC1/S04`：关闭时在事务前拒绝。
- `C06/AC1/S05`：关闭时不留下已受理 turn。
- `C06/AC1/S06`：关闭时不留下已受理回执。

### C06/AC2

来源：[issue #41](https://github.com/hrygo/SpeechNexus/issues/41)，正文更新时间 `2026-10-25T07:36:02Z`；原文 SHA-256 `b66374526459fb88498e2b6a0e166b8e8a5aea08116e879256cee2d783c80408`。

> 纪要固定 sealed 转写、模板/模型/绑定版本，失败不改成功转写，任务由受控派发器完成并保留原意图恢复。

- `C06/AC2/S01`：纪要固定 sealed 转写。
- `C06/AC2/S02`：纪要固定模板版本。
- `C06/AC2/S03`：纪要固定模型版本。
- `C06/AC2/S04`：纪要固定绑定版本。
- `C06/AC2/S05`：纪要失败不改成功转写。
- `C06/AC2/S06`：任务由受控派发器完成。
- `C06/AC2/S07`：纪要任务保留原意图恢复。

### C06/AC3

来源：[issue #41](https://github.com/hrygo/SpeechNexus/issues/41)，正文更新时间 `2026-10-25T07:36:02Z`；原文 SHA-256 `9acbe011e52f45cbeec357af4d0ac5d8a13c9ae847c1ccae0313c890a7e4f7bf`。

> 助手 turn 以真实 speech_job_id 和已发布声音推进冻结状态机，启用态成功、取消、失败和重放均有真实 HTTP/持久化证据。

- `C06/AC3/S01`：助手 turn 由真实 speech_job_id 推进。
- `C06/AC3/S02`：助手使用已发布声音。
- `C06/AC3/S03`：助手遵守冻结状态机。
- `C06/AC3/S04`：启用态成功有真实 HTTP 与持久化证据。
- `C06/AC3/S05`：启用态取消有真实 HTTP 与持久化证据。
- `C06/AC3/S06`：启用态失败有真实 HTTP 与持久化证据。
- `C06/AC3/S07`：启用态重放有真实 HTTP 与持久化证据。

### C06/AC4

来源：[issue #41](https://github.com/hrygo/SpeechNexus/issues/41)，正文更新时间 `2026-10-25T07:36:02Z`；原文 SHA-256 `0a82a6393a87ea3a185b7a38e2f989a2fa0feeb5e40f59c3b1d2e3a854afe7da`。

> 插话先停旧播放，旧计算按底座停止/释放事实协调；文本/TTS/播放器 generation 固定关联，迟到音频不串播。

- `C06/AC4/S01`：插话先停旧播放。
- `C06/AC4/S02`：旧计算按底座停止事实协调。
- `C06/AC4/S03`：旧计算按底座释放事实协调。
- `C06/AC4/S04`：文本与 TTS generation 固定关联。
- `C06/AC4/S05`：TTS 与播放器 generation 固定关联。
- `C06/AC4/S06`：迟到音频不串播。

### C06/AC5

来源：[issue #41](https://github.com/hrygo/SpeechNexus/issues/41)，正文更新时间 `2026-10-25T07:36:02Z`；原文 SHA-256 `95c31f29b65483516bed5cb2955c78b0f3c0e2b1cab97b8404ccef2fdb2eeaed`。

> 独立保存/导出及实际质量可验；应用/代理/浏览器/观测无越界内容出站，同设备占用纳入父预算，HTTP 取消不代替远端停止。

- `C06/AC5/S01`：独立保存可验。
- `C06/AC5/S02`：独立导出可验。
- `C06/AC5/S03`：实际质量可验。
- `C06/AC5/S04`：应用无越界内容出站。
- `C06/AC5/S05`：代理无越界内容出站。
- `C06/AC5/S06`：浏览器无越界内容出站。
- `C06/AC5/S07`：观测无越界内容出站。
- `C06/AC5/S08`：同设备占用纳入父预算。
- `C06/AC5/S09`：HTTP 取消不代替远端停止。

## C07

### C07/AC1

来源：[issue #42](https://github.com/hrygo/SpeechNexus/issues/42)，正文更新时间 `2026-10-25T07:36:00Z`；原文 SHA-256 `4685c09feb2d597cd3bdea1c5d546574486696386d9162243421f008060fb0c7`。

> render_unit 固定稿件/章节/角色/声音/词典/模型/profile/规划版本与 UTF-8 指纹，CAS 和生成历史关联可核对。

- `C07/AC1/S01`：render_unit 固定稿件版本。
- `C07/AC1/S02`：render_unit 固定章节。
- `C07/AC1/S03`：render_unit 固定角色。
- `C07/AC1/S04`：render_unit 固定声音版本。
- `C07/AC1/S05`：render_unit 固定词典版本。
- `C07/AC1/S06`：render_unit 固定模型版本。
- `C07/AC1/S07`：render_unit 固定 profile 版本。
- `C07/AC1/S08`：render_unit 固定规划版本。
- `C07/AC1/S09`：UTF-8 指纹可核对。
- `C07/AC1/S10`：CAS 可核对。
- `C07/AC1/S11`：生成历史关联可核对。

### C07/AC2

来源：[issue #42](https://github.com/hrygo/SpeechNexus/issues/42)，正文更新时间 `2026-10-25T07:36:00Z`；原文 SHA-256 `153ad8e00bc27f9cdfe3f6fc382c10b4d40bb1c2160316c9a68180ea61599ac8`。

> 改文仅影响目标单元、换声仅影响同角色，无关单元保留；旧/错 unit、Job、代次或指纹结果被拒绝。

- `C07/AC2/S01`：改文仅影响目标单元。
- `C07/AC2/S02`：换声仅影响同角色。
- `C07/AC2/S03`：无关单元保留。
- `C07/AC2/S04`：旧单元结果被拒绝。
- `C07/AC2/S05`：错 unit 结果被拒绝。
- `C07/AC2/S06`：错 Job 结果被拒绝。
- `C07/AC2/S07`：错代次结果被拒绝。
- `C07/AC2/S08`：错指纹结果被拒绝。

### C07/AC3

来源：[issue #42](https://github.com/hrygo/SpeechNexus/issues/42)，正文更新时间 `2026-10-25T07:36:00Z`；原文 SHA-256 `6f637d7c496a1ec8637c02d3bedfde788f1ec80f1f6e2f026e64c5c7e88c9e02`。

> 底座结果及独立保存副本按摘要/非空 source receipt 校验，可通过公开非 JSON 下载读取，不能以领域 ready 代替可导出。

- `C07/AC3/S01`：底座结果按摘要校验。
- `C07/AC3/S02`：底座结果按非空 source receipt 校验。
- `C07/AC3/S03`：独立保存副本按摘要校验。
- `C07/AC3/S04`：独立保存副本按非空 source receipt 校验。
- `C07/AC3/S05`：通过公开非 JSON 下载读取。
- `C07/AC3/S06`：领域 ready 不代替可导出。

### C07/AC4

来源：[issue #42](https://github.com/hrygo/SpeechNexus/issues/42)，正文更新时间 `2026-10-25T07:36:00Z`；原文 SHA-256 `6d556ff7a3f031931e02c13e07c1165f43ddf0d6ae84868487ff876063f0d0c7`。

> 独立 export worker 不调用推理引擎，CPU/内存/磁盘/并发有界并计入预算；固定清单/检查点，先 staging 校验再原子发布。

- `C07/AC4/S01`：独立 export worker 不调用推理引擎。
- `C07/AC4/S02`：export worker CPU 有界并计入预算。
- `C07/AC4/S03`：export worker 内存有界并计入预算。
- `C07/AC4/S04`：export worker 磁盘有界并计入预算。
- `C07/AC4/S05`：export worker 并发有界并计入预算。
- `C07/AC4/S06`：导出固定清单。
- `C07/AC4/S07`：导出固定检查点。
- `C07/AC4/S08`：先 staging 校验再原子发布。

### C07/AC5

来源：[issue #42](https://github.com/hrygo/SpeechNexus/issues/42)，正文更新时间 `2026-10-25T07:36:00Z`；原文 SHA-256 `e6478d5cd427db853b712898ae85e509e9f58ce810a651e53ec5a6c960fe22f1`。

> 取消/重试/续作不重复发布，缺章/空成品/局部失败可被识别，质量/试听/匿名确认记录固定关联版本。

- `C07/AC5/S01`：取消不重复发布。
- `C07/AC5/S02`：重试不重复发布。
- `C07/AC5/S03`：续作不重复发布。
- `C07/AC5/S04`：缺章可被识别。
- `C07/AC5/S05`：空成品可被识别。
- `C07/AC5/S06`：局部失败可被识别。
- `C07/AC5/S07`：质量记录固定关联版本。
- `C07/AC5/S08`：试听记录固定关联版本。
- `C07/AC5/S09`：匿名确认记录固定关联版本。

### C07/AC6

来源：[issue #42](https://github.com/hrygo/SpeechNexus/issues/42)，正文更新时间 `2026-10-25T07:36:00Z`；原文 SHA-256 `30f2cb7afeac601f50ae1744287c7a61ef08b0ef47051cc2ddd3de1d6f491acd`。

> 关闭工作台不影响底座，本包可独立于 C06 助手验收。

- `C07/AC6/S01`：关闭工作台不影响底座。
- `C07/AC6/S02`：C07 可独立于 C06 助手验收。

## C08

### C08/AC1

来源：[issue #43](https://github.com/hrygo/SpeechNexus/issues/43)，正文更新时间 `2026-10-25T07:36:09Z`；原文 SHA-256 `211275c40e09c6fd8b0a190a484e229d9a10dc66be49d43bbc2f3b9ac4cca428`。

> 实际页面覆盖相应 Gate 的业务闭环；物理设备采集/播放、权限、断网/重连、取消迟到、长时及完整键盘/焦点/小窗口/读屏有支持矩阵证据。

- `C08/AC1/S01`：实际页面覆盖相应 Gate 业务闭环。
- `C08/AC1/S02`：物理设备采集有支持矩阵证据。
- `C08/AC1/S03`：物理设备播放有支持矩阵证据。
- `C08/AC1/S04`：权限行为有支持矩阵证据。
- `C08/AC1/S05`：断网与重连有支持矩阵证据。
- `C08/AC1/S06`：取消迟到有支持矩阵证据。
- `C08/AC1/S07`：长时行为有支持矩阵证据。
- `C08/AC1/S08`：完整键盘有支持矩阵证据。
- `C08/AC1/S09`：完整焦点有支持矩阵证据。
- `C08/AC1/S10`：小窗口有支持矩阵证据。
- `C08/AC1/S11`：读屏有支持矩阵证据。

### C08/AC2

来源：[issue #43](https://github.com/hrygo/SpeechNexus/issues/43)，正文更新时间 `2026-10-25T07:36:09Z`；原文 SHA-256 `cc1bfdce4ba828779bff5b2313e5e2b3ef7d55b23133387e484f63b848eb6e19`。

> 版本化预检/确认、账本、对象擦除、启动重放和运行 sweeper 一致；未配置清理身份时返回 deletions_unavailable（503），不受理无法履行的删除。

- `C08/AC2/S01`：版本化预检与确认一致。
- `C08/AC2/S02`：删除账本一致。
- `C08/AC2/S03`：对象擦除一致。
- `C08/AC2/S04`：启动重放一致。
- `C08/AC2/S05`：运行 sweeper 一致。
- `C08/AC2/S06`：未配置清理身份返回 503 deletions_unavailable。
- `C08/AC2/S07`：不受理无法履行的删除。

### C08/AC3

来源：[issue #43](https://github.com/hrygo/SpeechNexus/issues/43)，正文更新时间 `2026-10-25T07:36:09Z`；原文 SHA-256 `382601a346a7be57e61cfae2f0ebb52bb224b68a0213428f953bd81bce07af05`。

> 独立副本不误删，对象/依赖图和 A07 恢复账本水位一致，重放完成后开放内容；恢复查询不承诺撤销已擦除内容。

- `C08/AC3/S01`：独立副本不误删。
- `C08/AC3/S02`：对象图与 A07 恢复账本水位一致。
- `C08/AC3/S03`：依赖图与 A07 恢复账本水位一致。
- `C08/AC3/S04`：重放完成后开放内容。
- `C08/AC3/S05`：恢复查询不承诺撤销已擦除内容。

### C08/AC4

来源：[issue #43](https://github.com/hrygo/SpeechNexus/issues/43)，正文更新时间 `2026-10-25T07:36:09Z`；原文 SHA-256 `482c725fc09b92a75f21fed4c734f9c5c48aefc80ed1fdfcb4d9378d76df9876`。

> LLM 关闭/不可用、内容出站/代理/观测及共享部署文案真实一致，日志不包含业务内容。

- `C08/AC4/S01`：LLM 关闭文案与行为一致。
- `C08/AC4/S02`：LLM 不可用文案与行为一致。
- `C08/AC4/S03`：内容出站文案与行为一致。
- `C08/AC4/S04`：代理文案与行为一致。
- `C08/AC4/S05`：观测文案与行为一致。
- `C08/AC4/S06`：共享部署文案真实一致。
- `C08/AC4/S07`：日志不包含业务内容。

### C08/AC5

来源：[issue #43](https://github.com/hrygo/SpeechNexus/issues/43)，正文更新时间 `2026-10-25T07:36:09Z`；原文 SHA-256 `180e70b58ed6e0ecd29cc8e56c1372af78631433b996a2be889924a9b2317509`。

> 生产清理 DSN/角色/迁移、Linux 后端/镜像与目标拓扑实际验证；支持矩阵、用户文档、运维/恢复交接齐备，既有 Minor/浏览器抖动的处理与未验证项明确。

- `C08/AC5/S01`：生产清理 DSN 实际验证。
- `C08/AC5/S02`：生产清理角色实际验证。
- `C08/AC5/S03`：生产迁移实际验证。
- `C08/AC5/S04`：Linux 后端实际验证。
- `C08/AC5/S05`：Linux 镜像实际验证。
- `C08/AC5/S06`：目标拓扑实际验证。
- `C08/AC5/S07`：支持矩阵齐备。
- `C08/AC5/S08`：用户文档齐备。
- `C08/AC5/S09`：运维交接齐备。
- `C08/AC5/S10`：恢复交接齐备。
- `C08/AC5/S11`：既有 Minor 处理明确。
- `C08/AC5/S12`：浏览器抖动处理明确。
- `C08/AC5/S13`：未验证项明确。
