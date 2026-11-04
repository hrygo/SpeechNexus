# 会议记录与纪要工作台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans inline；唯一一次fresh final review，Critical/Important一轮TDD修复，Minor逐项记录。持续授权覆盖现有产品实施，不重复审批或commit/push。

**Goal:** 正式工作台消费真实Meeting/Minutes协议，安全完成稿件关联、会议创建/打开、校对封存、批准profile纪要与刷新恢复，给出真实浏览器到隔离PG证据。

**Architecture:** 补C-owned WorkbenchStatus公开Minutes profile及Minutes源CAS，使用A现行生成器；沿既有WorkbenchClient同源有界transport扩展五方法。独立MeetingJournal/MeetingSession保存同标签页原请求/草稿，React只订阅状态和触发命令，浏览器使用C真实HTTP与独立PG及fake LLM。

**Tech Stack:** 既有React/Vite/TypeScript、共享tokens、生成校验、FastAPI/psycopg、Playwright。

**Spec:** `docs/speechnexus-product-architecture.md` §9.1/§9.4，ADR0003、`contracts/workbench-openapi.yaml`与本轮#35/#40/#41/#43任务。

## Global Constraints

- base/head7cd1887905dd57e8c647238416b6030049833e38，已交付250文件266ac1cd逐字节核对通过；九issue状态/任务先更新并回读，无未知改动。
- C-owned工作台契约与消费者同版改动；不写A生产root、共享迁移框架/锁/部署/生成器，不运行生产、设备、模型，不发新跨chat消息；不提交/推送。
- 页面不猜模板/模型；WorkbenchStatus新增可选`minutes_profile`（`template_revision`/`llm_model`），默认禁用/无profile不提供；旧响应缺该可选字段时不启用新纪要。无端点、prompt、binding、用量或凭据。
- MinutesCreateRequest新增可选`expected_source_revision`，新页面始终提交当前封存版本；服务原键先于源CAS/profile核验，未带字段的旧调用维持原行为，已提交请求与回执不能被改写。
- 不造会议列表或采集成功。使用已有项目/稿件分页、创建关联记录、显式编号打开及本标签页恢复；speech session/save_audio继续明确unsupported，会议要点/导出/realtime仍继续完整Goal。
- sessionStorage只保存本标签页草稿、选中来源/会议及原键原请求，不存凭据，不用localStorage。损坏/不可用/配额失败保留可复制内存输入并阻断提交，无静默重置或新键替代。
- 请求前持久化原键与完整body；未知结果不能清除/另建；原202只证明接受，随后GET当前事实。关闭LLM仍可重放原接受请求和读取结果；新请求只能使用服务端公开profile。
- 固定项目/稿件/会议/源版本与任务归属。校对与封存追加不可变版本；冲突保留草稿，明确比较后才能采用新基线，不能把旧正文自动套新head。
- 切换/离开中止读取并隔离晚到结果；已发写请求不能被解释为取消，原请求仍可恢复；存储失败前后关联状态原子更新。
- 继续现有共享token和文档工作台视觉：左侧项目/稿件，右侧主稿件与纪要分层；窄屏单列，左对齐，正文72ch/1.75，44px交互、可见焦点，无CDN/遥测/虚构进度。不新增字体或依赖，不改共享token值。

## Review Focus

1. 写入已提交但响应丢失后刷新/切换/关闭LLM，仍用同键同body恢复，不能生成重复会议/封存/纪要。
2. 存储quota/损坏时切换项目、会议、草稿及冲突状态不得交叉；新head出现须显式比较，不能借旧正文自动接受新版本。
3. 显示封存版本2后别人再封存为4，创建纪要必须409且不排错源；同键旧202重放先于当前profile/CAS。
4. 身份不匹配、删除/过期/失败/未知任务与迟到读取不能泄露/串联正文；纪要失败不改会议封存事实，输出视为文本。
5. 无profile/禁用/断网/空列表/分页/键盘/360px与卸载清理，拒绝明确且不以本地标识充当身份，浏览器不出站。

### Task 1: 批准profile、源CAS与生成客户端

**Files:** 修改C契约/共享样例并重生成；`application/meetings.py`、`postgres_meetings.py`、`interfaces/http_management.py`/`http_meetings.py`及对应Python/PG测试；扩展`web/workbench/src/manuscripts/workbench-client.ts`和测试。

**Interfaces:** `WorkbenchMeetings.llm_profile -> LlmProfile|None`；`create_minutes(...expected_source_revision:int|None=None)`及同事务store参数；WorkbenchStatus可选minutes_profile；客户端五方法`createMeeting/getMeeting/sealMeeting/createMinutes/getMinutes`，所有wire类型来自生成Contracts。

- [x] 先RED profile公开/禁用/非法值、源CAS/原键优先/配置变化及HTTP映射；实现源契约与生产者、重生成并验证Python/TS同版正负样例。
- [x] 先RED五客户端请求及身份/源/profile不匹配、重定向/异常、无默认值；实施同源8MiB transport复用并GREEN。
- [x] 针对性Python/契约、PG及客户端测试GREEN；记录本单元schema/生成边界，不改旧已提交事实。

### Task 2: 同标签页恢复与会话状态

**Files:** 新增`web/workbench/src/meetings/meeting-journal.ts`/`meeting-session.ts`与各自测试。

**Interfaces:** `MeetingJournal.load/save`严校版本/字段及跨记录关系；`MeetingSession`暴露稳定subscribe/getSnapshot及discover、moreProjects、selectProject、moreTranscripts、selectTranscript、create/open、seal、createMinutes/refresh、replay/retryStorage、useLatest/dismissRejected、dispose；公开state包含record、status、列表、busy/blocked/problem/conflict/verified。

- [x] journal先RED：原键/请求、精确Unicode/版本、错误关联/损坏/大小及存储失败；实施验证和原子clone持久化GREEN。
- [x] session先RED：创建/封存/纪要、原键未知恢复、LLM关闭恢复、CAS和显式比较、错误scope、切换/卸载/晚到、存储前后失败与取消读取；失效/删除实际消费者由Task3浏览器验收。
- [x] 实施会话状态并GREEN，验证输入与关联更新原子性；已拒绝请求须显式结束，未知请求不得提供新意图捷径。

### Task 3: 正式页面、真实浏览器与交接

**Files:** 新增`meetings/MeetingHost.tsx`/`meetings.css`及React测试，修改`App.tsx`/测试；新增`scripts/dev-env/workbench-meetings-probe.mjs`和`tests/integration/test_workbench_meeting_browser.py`；扩展C PG探针显式`--browser`；更新README双语/goal/index/交接。

**Interfaces:** MeetingHost注入client/storage供测试，生产lazy加载；Node浏览器脚本连接实际同源C app静态bundle，阶段事件经stdin/stdout与Python隔离PG/fake LLM协调，不增加生产控制路由。

- [x] React先RED：页面入口、来源/草稿、原键、纪要禁用/存储；实施页面GREEN，模型/模板只读展示，封存/结果与键盘交给同版真实浏览器验证。
- [x] build后实际Chromium→同源C HTTP→隔离PG：提交后合成丢回复、刷新/原键、LLM关闭恢复、fake任务完成/文本显示、删除/失效、窄屏/键盘/出站/异常与清理。明确只证明此拓扑，无真实ASR/设备/模型或生产资格。
- [x] Web五门禁、Python静态/依赖/unit+contract、28源契约/漂移、受影响PG（含--browser）；唯一fresh终审/一轮Important修复，Minor/Declined逐项记账。
- [x] 累计包保留旧250包，干净7cd apply/全字节/文档链接/index保护；交接、九issue状态/内部任务更新与最终逐字回读，完整Goal active。
