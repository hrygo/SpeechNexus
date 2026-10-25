# 团队 C — Meeting 与 Minutes HTTP 交接

核验日期：2026-10-24。base_sha=head_sha `7cd1887905dd57e8c647238416b6030049833e38`；无 C commit/push，保留全部既有工作。上一238文件包与SHA见[上一集成交接](2026-10-24-team-c-7cd-integration-handoff.md)及 `.runtime/team-c/team-c-before-meeting-http.*`；当前累计制品见 `.runtime/team-c/team-c-current.manifest.json`，不在本文件嵌入自身包hash。

完整范围继续遵循[团队 C Goal](2026-10-24-team-c-goal.md)，实施按[本单元计划](2026-10-24-team-c-meeting-http-plan.md)。C00–C08全部open，C03 planned、其余in-progress，Goal active，Gate不由C关闭。

## 已实现的业务边界

- C自有领域/应用/PG持久化Meeting记录，精确绑定同工作区/项目的既有Transcript；不启动采集或伪造来源。公开 `speech_session_id` 未有核验适配器、`save_audio=true` 未有保存链时返回422 `meeting_source_unsupported`，无记录提交。
- 显式封存追加不可变sealed revision，并与Meeting来源指针和原键回执同事务提交。后续校对不改旧稿，显式再封存保留首次ended_at并清除最新纪要关联；原键仍返回最初接受快照。
- Minutes enqueue与Meeting关联同连接/短事务；LLM不在HTTP请求或数据库事务中执行。模板/模型由部署profile固定，省略模型不注入客户端默认。LLM默认关闭，新请求503；配置后来关闭时仍可恢复已接受原键。
- `createMeeting`、`getMeeting`、`sealMeeting`、`createMeetingMinutes`、`getMeetingMinutes`使用生成HTTP请求/响应校验。创建/封存/202重放是原接受快照，GET为当前任务；失败/取消/过期/运行未知不改会议封存事实。输出不含内部绑定、prompt、usage或attempt token。
- `Idempotency-Key`按契约可选；省略即新意图。提交结果未知返回503 `outcome_unknown`，不自动重试；消费者须保存原键及请求。未带键的未知结果不能声称具备原键恢复。
- 项目/稿件删除预检纳入Meeting版本依赖；新会议或纪要关联使旧预检失效。ordinary阻断、cascade须确认全量依赖；tombstone关闭读取及原键内容重放。清理先移除已确认会议引用，再清理稿件/纪要内容，删除回执只保留hash/标识/时间。未知LLM执行仍由既有停止证据屏障阻止物理清理，不能将HTTP结束当作停止。

## 装配及迁移交给 A 串行接入

没有修改共享生产组合根、共享迁移框架、deploy、生成器或锁文件。owner在版本/删除/封存/MinutesTasks片段后显式应用 `meetings_migration_sql`，分别使用Meeting runtime/cleanup grants；运行角色无DDL、Meeting删除或回执改写权限。

同一部署必须一起启用 `PostgresDeletionUnitOfWork(uow, meetings=True)`、`PostgresDeletionCleanup(cleanup_uow, meetings=True)` 和新表迁移/grants，再向C工厂显式传入 `meetings=WorkbenchMeetings(PostgresMeetings(uow), approved_llm_or_none)`。默认不传时保留13项C1操作，显式装配为18项，OpenAPI只列实际注册项；状态端点反映实际Meeting/LLM配置。旧图漏依赖时新cleanup拒绝清理，不能将这种失败状态宣称为生产就绪。本单元不保证旧cleanup与新Meeting schema混用的滚动部署安全；A必须在启用业务写入前完成同版图/cleanup和迁移/grants的一致装配验收。

LLM worker仍通过既有 `MinutesTasks` 单独执行；实际批准的内网配置、停止对账和生产调度由接收方集成，不在HTTP中启动后台推理。生产启用前还需按部署变更程序核验迁移顺序、角色权限和回退；本单元未操作生产服务。

## 验证与证据

本单元先RED再GREEN，详细日志保留 `.runtime/team-c/meeting-http-7cd/`。Meeting领域/应用17项、HTTP边界与错误契约135项已通过；隔离PG覆盖提交后合成回执丢失、并发同键、CAS、跨scope、回滚/取消、精确封存、任务发布/过期与删除屏障。回执丢失是实际数据库提交后的故障注入，不代表进程崩溃或跨机故障资格。

完整Python unit/contract **2867 passed**（50.17s，无测试skip），format513、ruff、mypy381源文件及7项依赖边界通过；28源契约/生成漂移通过。Web五门禁通过，**1206=contracts792+Admin79+Workbench335**，无测试skip。共享HTTP样例从357扩展到413，由Python/TS同版消费。隔离PG **293 passed**（16.88s，组合覆盖94.29%，无测试skip），含5项实际loopback流程；独立pg_ctl确认停止且无pid。唯一fresh终审 **Critical0/Important0/Minor2**，独立152项通过、30文件hash匹配。根代理维持两项Minor并延后：两会议共享稿件的组合并发回归、坏成功输出的HTTP校验故障注入；无Important修复或二次审查。8项Declined逐项裁决保留生产/真实推理/网络/跨节点/页面设备等未验证范围；不将源码静态检查当动态穷举或mutation分数。累计250文件已在干净7cd基线apply-check/apply、逐字节、333本地链接和真实index保护中通过；最终SHA与所有字节以当前manifest为准。

可复现入口（仓库根目录；使用已有PostgreSQL工具，不访问共享实例）：

```bash
uv run pytest tests/unit tests/contract -q
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run lint-imports
make contracts-check contracts-drift
uv run python scripts/dev-env/workbench-pg-probe.py --pg-bin "$(pg_config --bindir)" --coverage
pnpm format:check
pnpm lint
pnpm typecheck
pnpm test
pnpm build
```

仅验证合成内容、fake LLM与实际loopback HTTP/隔离PG。Web门禁是生成消费者回归，本轮未新增浏览器交互或物理设备证据。会议页面、会议要点/导出、公开realtime状态机、音频保存、真实ASR/LLM、内容网络/恢复、Linux/GPU及完整C资格仍待交付；无新跨chat消息。
