# Meeting 与 Minutes 持久业务接口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans inline；唯一一次fresh final review。持续授权覆盖既有产品/契约的实施，不重复审批、commit/push或清理。

**Goal:** 在C自有事务中持久化Meeting、封存稿件并原子关联Minutes任务，公开五项已冻结接口，保留原键、来源与删除屏障。

**Architecture:** 独立Meeting领域对象/存储端口与应用服务，PG复用VersionUnitOfWork的连接/超时和MinutesTasks的任务事实；接口只消费生成parse/HTTP validator。生产root与共享迁移仍交A串行接入。

**Tech Stack:** 既有Python/FastAPI/psycopg、A生成workbench入口、隔离socket-only PG及真实loopback测试。

**Spec:** `contracts/workbench-openapi.yaml` Meeting/Minutes五操作，`docs/speechnexus-product-architecture.md` §9.1/§9.4、ADR0003，以及C00 #35/C05 #40/C06 #41/C08 #43和完整Goal。

## Global Constraints

- base/head7cd1887905dd57e8c647238416b6030049833e38；238文件83e96836旧包逐字节核对通过，开工issue状态/子任务已更新并回读。无未知/并行修改，已有改动保留。
- 不写A生产root/共享迁移/锁/生成器，不运行模型/设备或生产服务，不发新跨chat消息；本机隔离PG和合成loopback沿已有授权。
- 机器契约不足以表达实际错误时，先补C源操作响应/正负样例，再由A既有生成器重生成；不手改生成物、不伪造404/成功。
- 默认workspace由组合配置固定。跨项目/工作区Meeting、Transcript与Minutes身份必须匹配；无登录/用户隔离或任意LLM端点。
- 本轮可核验来源为现有Transcript；speech_session_id缺公开核验/采集接线时明确unsupported，不捏造稿件或采集成功。save_audio=true在未有实际保存链时明确拒绝，不承诺会后重识别。
- 创建/封存/纪要请求先原键锁与意图比较，事务原子保存业务变更和原接受快照；重放先于当前CAS/profile核对，未知提交不自动重试。原键不能因内容清理被新意图复用。
- 封存追加新不可变sealed revision，原稿/历史不改；Meeting固定该版本，校对后纪要过期，失败不更改采集/封存事实。重新封存需显式新意图，不偷换原封存回执。
- Minutes关联与任务enqueue同事务，锁序project→transcript→meeting→task；不在事务内执行LLM/HTTP/对象写入。原202重放为接受快照，GET为当前事实；内部profile/prompt/binding/usage不泄漏到wire。
- LLM默认关闭；新意图只接受服务端固定profile/model/template，不注入客户端默认。已接受原键重放不因后来配置关闭而生成新任务；running未知不自动重派。
- Meeting依赖加入项目/稿件删除预检与完整确认；新增owner-only迁移和显式启用的依赖读取/cleanup，旧schema不自动探测或运行DDL。tombstone立即隐藏读取/重放，物理清理不遗漏标题/正文；执行未知的Minutes保留原清理阻断。

## Review Focus

1. 丢失COMMIT回执/配置变化后重放，不得新建Meeting/封存版本/Minutes任务或改变原接受状态。
2. 两个会议共用稿件、并发校对/封存/删除，必须固定同项目来源和锁序，不死锁、不将其他任务发布给当前会议。
3. 纪要失败/过期/未知运行，会议仍保留封存事实；GET与202原回执分别准确，不能暴露内部用量/提示词。
4. 新Meeting或新Minutes关联使旧删除计划失效；完整确认及清理不漏依赖/重放正文，也不绕过执行停止证据。
5. 禁用LLM、未核验session、save_audio、错误JSON/头/体积/超时/错误响应与取消必须明确失败，无后台假成功。

### Task 1: Meeting领域与应用边界

**Files:** 新增`domain/meetings.py`、`application/meetings.py`及`tests/unit/backend/test_workbench_meetings.py`。

**Interfaces:** `Meeting`不可变来源/封存/任务关联；`MeetingView(meeting, minutes)`；`MeetingStore.create/get/seal/enqueue_minutes/get_minutes`为原子持久端口，`WorkbenchMeetings`传入固定LLM profile/binding而不执行模型。

- [x] 先写领域/应用RED：来源身份、时间/版本、sealed/显式再封存、不同纪要成功/失败状态与不支持来源；禁用/profile与原键组合由PG步骤覆盖。
- [x] 实施对象与端口/应用，17项GREEN；domain不导入生成DTO/DB/框架，ruff/mypy374/import7通过。

### Task 2: PG原子存储与删除图

**Files:** 新增`infrastructure/meetings_schema.py`、`infrastructure/postgres_meetings.py`、`tests/integration/test_workbench_meetings_postgres.py`；修改`postgres_minutes_tasks.py`的同事务enqueue入口、`postgres_deletions.py`/`postgres_deletion_cleanup.py`的显式Meeting依赖支持；扩展现有PG探针。

**Interfaces:** `PostgresMeetings(versions)`实现Task1端口；owner执行Meeting迁移及最小角色grant，`PostgresMinutesTasks.enqueue_in_session(base,...)`共享同连接；删除adapter显式`meetings=True`才消费新增表。

- [x] 新PG用例先RED：并发同键/异意图、重启/真实提交未知、固定封存与原回执、跨scope、队列/成功/失败/过期、原子关联失败全回滚。
- [x] 实施短事务、业务约束/索引与无正文原回执；隔离PG GREEN，拒绝运行角色DDL/正文日志。
- [x] 删除回归先RED：新依赖令旧计划失效、ordinary阻断/cascade完整确认、tombstone与物理清理/重放、未知LLM清理仍阻断；实施显式图/cleanup并GREEN。

### Task 3: 五项公开HTTP与交付

**Files:** 新增`interfaces/http_meetings.py`、`interfaces/meeting_views.py`及HTTP单元/隔离loopback测试；修改C工厂`workbench/app.py`，补`contracts/workbench-openapi.yaml`操作错误及共享样例并运行现有生成器；更新交接/goal/index/README双语。

**Interfaces:** `create_meeting_router(service|None,...)`只注册create/get/seal/createMinutes/getMinutes；生成请求校验后映射Task1接口，响应严格parse/validate_response。

- [x] HTTP状态与每项稳定错误先写正负样例/RED，补C源响应并重生成；TS/Python消费者同版验证。
- [x] 实施路由与C工厂可选装配；原七版本/十三管理工厂保持原边界，显式装配后实际18项；读取/错误不缓存，无CDN或后台LLM执行。
- [x] 隔离真实loopback完整流程及异常/取消/未知/删除测试；全Python静态/依赖/unit+contract、受影响PG、28源契约/漂移与Web五门禁。
- [x] 唯一fresh终审，Critical/Important一轮TDD修复，Minor保留并记录每项Declined裁决；累计包干净基线/字节/链接/index保护、交接和issue任务回读。完整Goal/Gate不关闭。
