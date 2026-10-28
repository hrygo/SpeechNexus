---
title: "SpeechNexus 团队 C 执行目标"
status: superseded
version: "0.3.0"
date: 2026-10-24
---

# SpeechNexus 团队 C 执行目标

2026-10-26 收尾：本文件转为历史目标，后续工作由新负责人重新安排。[当前交接](2026-10-26-team-c-final-handoff.md)汇总已集成范围、待审 PR、风险与证据；以下原范围和 AC 保留，历史接力顺序不再自动执行。

## 目标与范围

依据[三团队实施计划](2026-10-23-three-team-delivery-plan.md)、[首轮任务卡](2026-10-23-agent-team-startup-briefs.md#4-c-队macbook-工作台与体验)和[总控 #6](https://github.com/hrygo/SpeechNexus/issues/6)，原目标为完成 C00–C08。范围包括工作台后端和独立 schema/对象区/迁移、AdminUI 和 Workbench、浏览器媒体、音色工作室、会议/提词、内网 LLM/助手、有声书及独立保存/导出、业务删除恢复与应用交付资格。

本文件保留原执行范围，不证明产品能力已实现或 Codex 原生 Goal 已完成。应用目标状态以工具返回为准，工作包和 Gate 状态以具名验收证据为准。

## 历史进展与原执行顺序（2026-10-24）

以下是 2026-10-24 的阶段记录：当时用户要求持续推进，原生 Goal 查询返回 `blocked`，文档曾为 `active`；两者不等同。现行文档状态为 `superseded`，不据此改变原生 Goal 或判定任何工作包/Gate 完成。

该阶段工作基线为 `4b08c396c0fe8fc43c7e85683e534befab6bb22b`。上一轮累计交付包为 338 路径（其 SHA-256 见上一轮 manifest），随后独立入口单元交付 347 路径累计包；本轮服务端提词持久化在该 347 路径之上新增 16 个文件、修改 14 个文件，最终 **363 路径**。当前累计补丁、逐文件哈希与 `git apply` 校验见 `.runtime/team-c/team-c-current.manifest.json`；为避免包内哈希自指，本文件不内联当前补丁哈希，最终哈希只写入 C 的九个跟踪 issue。保留全部现有及并行改动，未经明确授权不提交、推送或发送新跨 chat 消息。

已具名取证的阶段进展：

- 项目/稿件不可变版本、契约生成客户端、分页与删除生命周期，以及会议封存、纪要任务等后端切片已有各自交接和测试；完整生产迁移及恢复资格仍待。
- Admin 任务中心、Workspace 只读消费，Workbench 文件转写/配音、编辑、会议、删除、音色创建及实时 ASR/TTS 消费已有分段实现。浏览器证据分别限定为合成 HTTP/WS/媒体或隔离 PG，不能推及真实模型、物理设备或全链路生产资格。
- C 独立 ASGI factory 已装配 PostgreSQL 版本、删除和会议服务，具有显式配置、owner-only fresh schema baseline、启动只读检查、Host/Origin 边界，默认关闭 LLM。
- 独立入口单元已完成终审两项 Important 修复：真实隔离 PG 复现并阻断登录身份/生效角色及 DSN options 导致的启动检查与业务连接身份差异；合成敏感串复现并修复 URL 配置解析异常回显输入。修复后 Python unit/contract 3488 项、隔离 PostgreSQL 310 项、配置/入口相关 37 项及 2 项 Chromium 浏览器回归通过，静态与契约检查有对应日志。Web 源码在此单元未变，1604 项及五门禁为上一单元复用证据；未验证项与延期 Minor 见交接。

本轮已完成（2026-10-24）：

1. #35–#43 状态与正文任务勾选已更新，两项终审 Important 以待修项写回正文；九 issue 保持 open/status:in-progress，父工作包和 G0–G5 未关闭。
2. 两项 Important 均先观察失败测试、再修改实现、最后运行相关完整回归：入口与业务连接统一 `SET ROLE NONE` 并显式设置 `search_path`，预检要求 `session_user = current_user`；配置解析异常一律转为固定消息。
3. 已记录 5 秒启动检查是应用级取消目标、驱动清理可能延长，不作为硬墙钟总期限；更新交接、累计补丁/manifest，并逐字回读九个 issue 最终正文。

本轮已完成（2026-10-24 服务端提词持久化）：

4. 将 C05 已声明但未接入的两项提词操作接入独立 PG：按项目保存稿件版本、可见位置与手动接管；严格更旧的 `script_revision` 409、同 revision 允许手动后退、原键返回原接受快照、无状态或已 tombstone 项目 404。新增第六份 owner-only 片段（状态表 + 回执表，随项目级联），运行角色仅两表 SELECT/INSERT 与状态表四列 UPDATE，启动检查新增四组真实 PG 反例。
5. 契约补齐这两项操作同类变更应有的传输错误状态（GET 400/408/413/500/503，PUT 422/408/413/415/500/503）并明确 GET 的 404 语义；workbench surface 15→16 路径、18→20 操作，profile 增加两个 schema，生成产物由既有流水线重建。
6. 证据：Python unit/contract 3577、隔离 PostgreSQL 317（含真实 lifespan/HTTP 持久化、另一次前进后原键仍返回原快照、5 路并发不丢更新、旧稿拒绝、tombstone 404 与 cleanup 级联计数 0），`tests/contract` 1491，Web/契约 1657 与五门禁；静态、契约与生成漂移门禁通过。一次 fresh 终审为 Critical 0 / Important 0 / Minor 4，M1/M2/M3/M5 已收紧并回归，M4 记为接受的 Minor。公开实时接线、真实语音跟随/设备、Linux/GPU 及生产迁移仍待，完整 C05/C08 资格未完成；最终补丁哈希待授权后写入 #35–#43。

7. 完成 C06 独立对象区与项目资产独立保存单位：契约接入 `saveProjectAsset`/`getSavedAsset`（workbench surface 16→18 路径、20→22 操作，profile 23→25 schema）并补齐两条操作同类变更应有的传输状态；新增领域/应用端口（store、object zone、speech artifact source）、第七份 owner-only 片段 `saved_assets`+`saved_asset_receipts`（TABLES 22→24、`SCHEMA_REVISION` 2→3）、MinIO 对象区适配器（显式来源、固定 origin、无重定向、阻塞 I/O 移出事件循环）与公开 Artifact 消费适配器（团队 A 生成校验器 + 有界私有 spool + receipt/长度/摘要/过期校验）。
8. 保存语义：对象先写入私有对象区（项目内内容寻址键），再发布数据库引用；同键重放不回源也不重复写对象，意图不同的同键 409；源身份/receipt/摘要/过期失败不落行、不落对象；项目删除在行提交后按项目前缀幂等擦除对象且可重试。
9. 证据：Python unit/contract 3655（新增 26 项单元）、隔离 PostgreSQL 322（新增 5 项，覆盖真实 MinIO 字节与摘要一致、真实回环 HTTP 源、重放不回源、源失败不写对象、行级联 + 前缀擦除及 worker 重试幂等）、`tests/contract` 1543、Web/契约 1709（contracts 1074 + admin 110 + workbench 525）与五门禁；静态、契约与生成漂移门禁通过。未完成：内容下载 `downloadSavedAsset` 因共享生成器仅支持 JSON 响应体而未纳入已验证传输表面（owner A，解除条件为生成器支持二进制响应体或 A 确认不校验内容表面）、生产删除 worker 入口未接线、本单元独立审查未执行。

10. 完成 C07 有声书 books/chapters 持久化：契约接入 `createBook`/`getBook`/`createChapter`/`getChapter`（workbench surface 18→22 路径、22→26 操作，profile 25→29 schema）；新增 `domain/books.py`、`application/books.py`、`infrastructure/books_schema.py`+`postgres_books.py`（TABLES 24→27、`SCHEMA_REVISION` 3→5）、`interfaces/book_views.py`+`http_books.py`；项目行作为唯一串行点，回执按资源分域，章节位置冲突显式 409（新增稳定错误码 `ordinal_conflict`）。
11. 处置 C06 独立审查结论（Critical 0 / Important 4）：对象区擦除改为有界多趟并把可见性闸门放在任何下载与对象写入之前；清理夹具用另一条连接证明「行提交后才擦对象」；`runtime_schema.cleanup_grants_sql` 统一清理角色授权；预检失败也释放长连接句柄；补充可判别的对象区与可见性用例。
12. 证据：Python unit/contract 3775（新增 12 项 books 单元 + 2 项对象区单元）、隔离 PostgreSQL 325（新增 3 项 books 集成：键回放/作用域、4 路并发同位置只落一章、tombstone 不可见 + 真实 worker 级联删净）、`tests/contract` 1648、Web/契约 1814（contracts 1179 + admin 110 + workbench 525）与五门禁；静态、契约与生成漂移门禁通过。本单元独立审查结论 Critical 0 / Important 1 / Minor 6（`review-books.md`，12 变异逐字节还原、384 文件哈希全命中）：唯一 Important「`source_text` 的 NUL 输入被报成 503 存储故障」已修（新增 `_optional_text` 守卫 + 单元与真实 PG 用例），并补上「启动被拒后释放 httpx/MinIO 句柄」与「清理角色使用组合授权」两项测试钉住；回执作用域一致性下沉到 DDL CHECK。未完成：渲染单元持久化（需先有 casting/model-binding 注册表，拒绝假 pin）、导出、生产删除 worker 入口；接受的 Minor 与未验证项见交接。

15. 完成业务删除恢复：启动先重放删除账本再开放内容（ADR 0005）。新增 `application/recovery.py`（有界分页重放、失败即拒绝启动、账本超界拒绝而非空转）、清理适配器 `pending()`、设置项 `cleanup_database_url` 与启动前置校验 `check_cleanup_identity`（清理身份必须是窄权限登录身份；owner、owner 成员、BYPASSRLS、`pg_read_all_data`/`pg_write_all_data` 成员一律拒绝启动）；恢复在 lifespan 中先于 `yield` 执行。
16. 处置恢复单元独立审查（Critical 1 / Important 1 / Minor 6）：**对象擦除补独立完成证据**——`deletion_outbox.objects_erased_at`（`SCHEMA_REVISION` 5→6、清理角色加对应 UPDATE 授权），`run()` 仅在对象扫掠成功后才在独立事务写该标记，`pending()` 配了对象区时取「行未擦 **或** 对象未擦」，从而消除「行已擦、对象未擦」被当成已完成的 fail-open 窗口（对象 I/O 仍在数据库事务之外）；身份门补预定义宽权限角色成员判定；补测钉住 `SET ROLE NONE` 与 schema USAGE 两条守卫。
17. 证据：Python unit/contract 3781、隔离 PostgreSQL 336（恢复集成 5→9：新增死端口对象区「第二次启动仍拒绝」回归、宽权限角色成员拒启、无 USAGE 拒启、良性角色切换按登录身份结算，均对真实 PostgreSQL 与真实 MinIO）；变异全部被杀死（跳过重放 / 跳过身份校验 / 重放已完成后删除 / 对象半还原为只看行 / 删预定义角色判定 / 删 `SET ROLE NONE` / 删 USAGE 守卫），文件按字节还原并复核 sha256。18. 关闭恢复单元的 Important 1：删除承诺与擦除身份绑定——未配置 `cleanup_database_url` 时 `create_runtime_app` 传 `deletions=None`，删除契约路由以既有 `deletions_unavailable`(503) 拒绝，部署无法受理它无法履行的删除；配置后三路由恢复（真实 HTTP 受理仍为 202）。变异「去掉该门」被杀死。证据：Python unit/contract 3781、隔离 PostgreSQL 337（恢复集成 10 项 + runtime 集成新增清理角色）。19. 冻结渲染单元（有声书固定依赖）的**来源钉住**设计，作为实现前置。契约输入只有 `ordinal`/`text`/`voice`/`model`/可选 `pronunciation`/`profile`，而领域 `RenderDependencies` 还需 `original`/`working`/`text_plan`/`chapter_id`/`role_id`/`model_binding`。逐项定源：`chapter_id` 取路径；`text_plan`/`original`/`working` 取该章节行的 `text_revision`（章节当前不可改，三者相等；字段已为将来章节编辑分叉预留）；`role_id` 是 C 公开选角语义（一本书一个旁白角色，改角经 `Book.casting_revision`），其钉住值来自请求的 `voice`；**`model_binding` 必须来自外部**——A 的 runtime governance 拥有模型→引擎绑定修订，C 自建注册表给「首次见到的模型」发 revision 1 等于伪造 C 从未核验过的绑定历史，故拒绝。选定方向：workbench 契约的 create/update 请求增加显式绑定引用（`ResourceReference`），由部署/AdminUI 从 A 的公开治理面取值，C 原样存储并参与摘要。**阻塞分级更正（第 14 轮）**：该字段的契约形态与持久化实现**不依赖 A** —— C 自己拥有 workbench 契约，字段定义为引用类型（id + revision），测试用合成引用即可；A 的公开绑定查询只影响**生产取值来源**与端到端验收，不构成实现前置。因此渲染单元的实现无需再等待 A，仅其生产取值与真实验收保留为外部依赖。
20. 用真实隔离 PostgreSQL 钉住清理身份的 `SET search_path = pg_catalog` 守卫（审查中唯一存活的变异）。影子 schema 伪造 `pg_roles`/`pg_namespace`/`pg_database`，被伪造对象是**非超级用户**登录：它持有 owner 角色成员资格与清理授权，真实 catalog 判定其不安全，影子行却声称安全。两轮实测修正的要点：PostgreSQL 仅在 `pg_catalog` 未被显式列出时隐式前置它，攻击形态必须写作 `<影子>,pg_catalog`；探针 owner 是超级用户（实测 `rolsuper=true`），因 `pg_has_role(任意角色,'MEMBER')` 恒真而无法被伪造成「安全」，故第一版不具判别性。证据：有守卫时用例通过，去掉守卫的变异被杀死（`DID NOT RAISE RuntimeStartupError`）。
21. 完成渲染单元契约切片：`RenderUnitCreateRequest` 必填 `model_binding`（`ResourceReference`）、`RenderUnitPatch` 可选同名引用、`RenderUnit` 响应回显并列入必填；同步三处消费者 fixture。证据：`make contracts-generate` 重建成功、`make contracts-check` 28 文件通过、`tests/contract` **1648 passed**、ruff/mypy/lint-imports/`contracts-drift`/`git diff --check` 与 Web 五门禁全绿。踩坑：`RenderUnit.required` 为块列表，三处 fixture 缩进不同，需窗口定位 + `ruff format` 收尾。
22. 完成渲染单元持久化底座：新增 `render_units` + `render_unit_receipts`（章节复合外键、位置唯一、每章唯一 id 供收据复合外键、五态 status、钉住列含新契约字段 `model_binding`、`dependencies_sha256` 格式 CHECK、pronunciation/profile 成对 CHECK）；`SCHEMA_REVISION` 6→7、`TABLES` 27→29、`bootstrap_sql`/`runtime_grants_sql` 与 `_UPDATE_COLUMNS` 同步（运行角色获精确 UPDATE 列集）。证据：真实隔离 PostgreSQL 集成 28 passed（含 bootstrap/预检/授权/删除级联）、unit/contract 3781、探针 339、静态全绿；变异「授权列集缺 status」被杀死（预检拒绝启动）。
23. 完成渲染单元应用层与持久化适配器：`RenderUnitStore` 端口 + `WorkbenchRenderUnits` 三命令（空补丁前置拒绝、幂等键校验）；`PostgresRenderUnits` 由存储行重建依赖并断言摘要一致，create 取章节真实 `text_revision` 作三钉、写单元+收据，get/update 经工作区校验与 `render_cas`，UPDATE 仅触碰授权列集。证据：真实 PG 集成 4 项（含同键重放/冲突、跨工作区拒绝、CAS 冲突、无变化拒绝、同章多单元）→ 探针 **343 passed**；unit/contract 3781；ruff/mypy(480)/lint-imports 全绿；变异「同键接受任意 intent」「跳过 CAS」均被杀死。未完成：公开 HTTP 三操作与组合根装配、TXT 导入、保存副本、质量确认、独立导出 worker。
24. 渲染单元绑定面与公开 HTTP 三操作：修复「schema/路径未进 `contracts/bindings.json`」缺口（worked surface 新增 3 schema + 2 路径，generations 读路径保持未绑定）；契约驱动修正 create 仅带 `chapter_id`（由章节解析所属项目并对项目行加锁）；新增视图与 `http_render_units.py` 三操作（409/404/422、outcome_unknown/store_unavailable）并接入 `app.py`/`runtime.py`；补 12 条正负契约样例、路径计数 22→24。证据：unit/contract 3793、真实 PG 35、探针 343、contracts-check/drift、lint-imports、ruff/mypy(482)、Web 五门禁全绿。未完成：保存副本、TXT 导入、导出 worker 与页面接线。
25. 渲染单元 HTTP 真实运行时集成与命令单元测试：`test_workbench_render_units_http.py`（真实 uvicorn/信封/响应校验，create 201+重放同 id、get 200、patch→stale、旧修订 409、缺绑定 400 且回执无敏感值、未知 404、同键异内容 409、空补丁被信封 400）与 `test_workbench_render_unit_commands.py`（键 8/128 边界、空补丁前置拒绝）。事故：曾覆盖同名既有领域单测文件致 `tests/unit` 少收集 65 项，已按冻结清单字节还原（sha256 一致）并将新测试改名，还原后 unit+contract **3795 passed**（=3793+2）闭合；探针 **345 passed**。
26. 助手回合持久化底座：新增 `assistant_schema.py`（`assistant_turns` 五态、项目级级联、voice 钉住、profile 成对、`error` jsonb、`UNIQUE (project_id, id)`；`assistant_turn_receipts` 复合外键绑定），`UPDATABLE_COLUMNS` 7 列同时驱动授权，`SCHEMA_REVISION` 8→9、`TABLES` 30→32，bootstrap 与授权装配。证据：真实 PG 预检 30 passed、变异「删授权列 `error`」被杀死、unit/contract 3795、探针 345、静态全绿。未完成：领域、应用、适配器、HTTP 三操作与真实行级测试（下一步）。
27. 助手领域与应用：`domain/assistant.py` 五态状态机（failed ⇔ error、queued 无 output、时间 aware 单调、参数校验先于 CAS）与 `application/assistant.py`（端口 + `WorkbenchAssistant`；**禁用时在开事务前拒绝**，不留下已受理行；键与 profile 引用校验）。证据：新增 6 项单测，`tests/unit tests/contract` **3801 passed**（3795+6 闭合），ruff/mypy(488)/lint-imports 全绿；探针沿用 345（本轮不被集成面覆盖）。未完成：适配器与行级 PG、HTTP 三操作与绑定面、默认关闭 503 真实 HTTP。
28. 助手适配器与行级真实 PG 证据：确认擦除为级联驱动、助手表无注册缺口；`postgres_assistant_turns.py`（项目持锁 + 幂等收据 + `FOR UPDATE` + 领域 CAS + 仅更新 status/revision/updated_at + jsonb error 往返校验）；4 项行级集成（列级往返/重放与冲突/跨工作区 404/取消与重复取消/失败读回/**擦除后回合与收据行归零**）。证据：探针 **349 passed**、unit+contract **3801 passed**、变异被杀死（文件字节还原 `f0c4ad05`）、ruff/mypy(490)/lint-imports 全绿。装置发现：变异后必须清 `__pycache__`，否则后续运行可能复用变异体字节码（已纳入流程）。未完成：HTTP 三操作与绑定面、执行器写入路径。
29. 助手 HTTP 面：契约核对纠正两处假设（取消无 `expected_revision`、响应不含 model/profile）；绑定 2 schema + 3 路径（计数 24→**27**）+ 10 样例；取消改为**按状态幂等**（`FOR UPDATE` 后以当前行修订 CAS，200=本次取消 / 202=已终态）；`assistant_views.py` 与 `http_assistant.py`（`LlmError` 稳定码映射）；领域强化 succeeded 必须带任务号、error code 收紧为线上 pattern；`runtime.py` **不注入 LLM**（默认关闭，待部署提供内网 LLM）。证据：探针 **351 passed**、unit+contract **3818 passed**、两处变异被杀死（零落库与错误码）、文件字节还原 `a0098d44`、ruff/mypy(494)/lint-imports 全绿。未完成：执行器写入路径；启用态真实 HTTP 成功路径需部署内网 LLM，当前不可达。
30. 终审两项 Important 复核与九个 issue 正文更新：**登录身份/生效角色/DSN options 差异**由真实隔离 PG 用例 `test_dsn_role_switch_cannot_change_checked_or_business_identity` 阻断（启动检查 `SET ROLE NONE` + `search_path=pg_catalog` + `BEGIN READ ONLY` 后要求 `session_user=current_user` 且无高权限），同组 **19 passed/5 deselected**；**URL 配置解析异常泄露输入**由合成串用例覆盖三种畸形形状，**28 passed**。#35–#43 各追加「最新执行状态（2026-10-24 · 团队 C 第 37 单元）」；只勾选 #35 独立 schema/对象区/revision/冲突一项，其「生产迁移/组合根/公开消费者/跨模块副本」后续项保持未勾选；#41/#42 已交付半与未实施半点名分列、不勾整条；九个 issue 保持 OPEN + `status:in-progress`，未关闭父工作包、未触碰 G0–G5。
31. 书导出作业持久化（C07 独立导出基础）：先以脚本比对「已冻结未绑定」操作（7 条）选定无 A 依赖的导出一族；契约核对确认七态、`artifact ⇔ succeeded`、无 revision 字段（故取消按状态幂等）与 `expected_casting_revision` 语义；新增 `domain/exports.py`（七态状态机 + 进度/时间/配对不变量）、`application/exports.py`（端口 + 前置校验，参数名避开内建 `format`）、`exports_schema.py` + `postgres_exports.py`（双 FK 级联、jsonb 选择集/产物/错误、项目行持锁、陈旧修订拒绝、**选择集只能含本书单元**、幂等收据）；运行面 revision **10**、表 **34**、UPDATE 白名单 6 列。证据：8 项单测 + 5 项真实 PG 行级测试（含擦除归零）、**三处变异全部被杀死**（字节还原 `b7fffdc0`）、探针 **356 passed**、unit+contract **3826 passed**、ruff/mypy(500)/lint-imports 全绿。未完成：导出 HTTP 三操作与绑定面（27→30）、事件流/下载（待 A 侧非 JSON 响应体）、导出 worker 与对象区发布。
32. 导出 HTTP 三操作与绑定面（C07）：绑定 3 schema + 3 路径（路径计数 27→**30**）+ 12 样例（契约套件 1682）；`export_views.py` 投影不含私有 `unit_ids`、artifact 仅在 succeeded；`http_exports.py` create 202 / get 200 / cancel **200 真取消 · 202 已终态幂等**，错误码映射含 `revision_conflict`/`invalid_change`/`resource_not_found`/`idempotency_conflict`；`app.py`+`runtime.py` 接线。证据：8 项投影单测 + 2 项真实 HTTP 端到端（create 202 固定 manifest、同键重放、get、取消 200 与重复 202、陈旧修订 409、越书 404、缺 format 400）、**两处变异被杀死**（取消状态取反、manifest_revision 未转字符串）、探针 **358 passed**、unit+contract **3846 passed**、ruff/mypy(504)/lint-imports 全绿。剩余未绑定 4 条：`downloadSavedAsset`/`downloadBookExport`/`streamExportEvents`（需非 JSON 响应体，owner A）与 `generateRenderUnit`（待 A Job API）。未完成：导出 worker 与对象区发布、前端导出入口。
33. 导出 worker 写入路径与对象区发布（C07）：依 `ExportEvent*` 事件族补齐五条由契约规定的转移（start/progress/complete/fail/expire），适配器统一 `_advance`（`FOR UPDATE` + 领域转移 + 单条精确 UPDATE，与 GRANT 白名单一致），artifact/error 的 jsonb 编解码逐字段对应；无租约列而以行锁 + 状态拒绝保证不双产出。证据：1 项服务单测 + 2 项真实 PG（转移全链路落库、五方法未知 id 均 404、**真实 MinIO 发布回路**含字节回读与前缀清理，标注合成内容）、**两处变异被杀死**（artifact 写 NULL 触发 CHECK、负数进度校验失效，字节还原 `4f8d67f9`/`52cd8156`）、探针 **360 passed**、unit+contract **3847 passed**、ruff/mypy(504)/lint-imports 全绿。未完成：导出内容生产（渲染单元→音频→打包，依赖 A 侧 TTS）、事件流与下载（owner A 非 JSON 响应体）、前端导出入口。
34. 导出 worker 循环与 producer 端口（C07）：`ExportWorker.run_once` 走认领→生产→发布→结算，`ExportProducer` 把内容生产隔离为单一依赖，**未配置 producer/zone 时 fail-closed 完全不触达 store**；适配器新增 `claim_queued_exports`（`FOR UPDATE SKIP LOCKED`）与 `settle_cancel_export`。取消为**协作式**（仅在下一次进度上报时生效，不立即中断单单元生产、无强制终止预算，作为已知限制记录）；并纠正语义：**producer 单方面中止而无取消请求时必须记为 `failed/aborted`，不得谎称 cancelled**（该断言最初写成 cancelled 并失败，据此修正实现）。失败仅记稳定码 `unavailable`，provider 原始文本不外泄。证据：7 项 worker 单测 + 5 项真实 PG + 真实 MinIO 集成（端到端 succeeded 且对象字节回读、二次运行认领 0、真实取消 cancelled、无请求中止 failed/aborted、失败不发布对象、未配置保持 queued）、**三处变异全部被杀死**（不进入取消分支 / 吞取消冲突继续生产 / 摘要输入错误，字节还原 `0b2ec0f2`）、探针 **365 passed**、unit+contract **3854 passed**、ruff/mypy(507)/lint-imports 全绿。未完成：真实 producer（owner A，TTS 流水线）、事件流与下载（owner A）、前端导出入口。
35. Workbench 有声书导出前端（C04）：前端此前**完全没有书目界面**，本轮补齐导出闭环——`workbench-client` 增 5 方法（固定 `expected_casting_revision`、queued 且无产物、格式/书目不得漂移）、`book-export.ts` 会话（进度只前进、终态冻结、迟到应答不覆盖、取消应答须同源、dispose 停止更新）、`BookExportPanel`（发起/进度/取消/摘要/失败码，**明确声明下载入口未接入且不渲染下载链接**）、`BooksHost`（项目→创建/打开书目→导出）、App 新增标签页；因**契约无 listBooks**，界面按 sessionStorage 记住的书目 ID 或显式输入寻址并如实说明。证据：客户端 4 + 会话 7 + 面板 4 + 宿主 3 = 18 项新测试（含 RTL/StrictMode 与无下载链接断言），五门禁全绿（workbench 543 = 525+18、整仓 `pnpm -r test` exit 0、build 通过）；**并修复两处因先前绑定而失效的消费者断言**（类型与运行时各一处 `createAssistantTurn` → 真正未绑定的 `downloadSavedAsset`），补齐导出族 4 条响应样例后 `tests/contract` **1686 passed**（1682+4）。未完成：章节/渲染单元界面、导出下载（owner A）、书目列表操作（需产品确认）。
36. listBooks 契约+后端+真实 PG（C07）：证据显示 books/chapters/render-units **无任何 list 操作**，第 42 轮前端只能本地记 ID；本轮补第一条：`get: listBooks`（`x-nexus-pagination` live_keyset + `BookList` schema + 绑定 + 5 条样例），`default_limit` 上移 `http_ingress` 并由管理路由委托，`ListingKind`/cursor 白名单增 `books`，服务层 `validate_listing`+多取一行，适配器**单语句**取父存在性/墓碑+子行并加 `workspace` 与项目墓碑谓词，路由对齐 limit 解析与 `invalid_cursor`，`app.py` 共享 `CursorCodec`。证据：2 项 HTTP 单元（分页/游标/越界与坏游标在边界被拒且不触达 store/404）+ 2 项真实 PG（最新在前、他 workspace/不存在/墓碑项目不可见、空项目空页）、**三处变异全部被杀死**（字节还原 `ffff0bbc`）、探针 **367 passed**、unit+contract **3865 passed**、ruff/mypy(507)/lint-imports 与五门禁全绿。教训：探针须与 MinIO 环境变量同一次 shell 导出。未完成：`listChapters`/`listRenderUnits` 与前端消费。
37. listChapters 契约+后端+真实 PG（C07）：`/v1/workbench/books/{book_id}/chapters` 增 `get: listChapters`（同一 live_keyset 语义 + `ChapterList` + 5 条样例，并在描述中固化"列表顺序=创建时间倒序、阅读顺序=ordinal"），`ListingKind`/cursor 白名单增 `chapters`，服务层 `list_chapters`，适配器**单语句**（books⨝projects 取可见性与项目墓碑 + LATERAL keyset 页），路由注册与分支补齐；章节列表投影与 `getChapter` 一致（仅元数据，不扩大暴露面）。证据：1 项 HTTP 单元 + 1 项真实 PG + **四处变异全部被杀死**（workspace/墓碑/keyset/排序；字节还原 `48709150`）、探针 **368 passed**、unit+contract **3871 passed**、ruff/mypy(507)/lint-imports 与五门禁全绿。教训：非 f-string 的 schema 占位符必须写 `{s}`（写成 `{{s}}` 会让 Postgres 报语法错误）；变异锚点重复时须补上下文重跑而非跳过。未完成：`listRenderUnits` 与前端消费。
38. listRenderUnits 契约+后端+真实 PG/HTTP（C07）：`/v1/workbench/chapters/{chapter_id}/render-units` 增 `get: listRenderUnits`（live_keyset + `RenderUnitList` + 5 条样例）；域层新增 `RenderUnitSummary`（线上不暴露创建时间，排序位点放在单元旁），适配器单语句取可见性+keyset 页并复用 `_UnitListingRow(_UnitRow)`，页内代次历史用第二条语句 `ANY(%s)` 一次取回（无逐行查询），路由以 `render_unit_payload(item.unit)` 保证列表项与直接读取一致。证据：1 单元 + 1 真实 HTTP（含列表项与 GET 逐字段相等）+ 2 真实 PG（含已记录尝试），**六处变异全部被杀死**（字节还原 `da81bbfa`）、探针 **371 passed**、unit+contract **3877 passed**、ruff/mypy(507)/lint-imports 与五门禁全绿；三条列表齐备，客户端不再需要记 ID。限制：代次历史的写入方属 speech 派发（owner A），测试用 owner DSN 直写真实 schema 状态，读路径为被测对象。教训：新增类须锚定类结束处插入，插入后用 mypy 复核。未完成：前端消费三条列表。
39. 前端消费三条真实列表（C07）：三条 list 操作就绪后把 Workbench 切到真实列表——`workbench-client.ts` 增 `books`/`chapters`/`renderUnits`（父域身份断言 + `page()` + cursor），`BooksHost` 用书目列表替换"打开已有书目 ID"表单（点击打开、创建后并入列表），新增按 ordinal 阅读顺序的章节列表与如实空态，并删除"书目列表接口尚未提供"的错误文案。证据：workbench 测试 **543 → 547**（+4）、admin 110、五门禁全绿（format/lint 含设计 Token/typecheck 0/test exit 0/build exit 0）；本轮仅 Web 变更，Python `unit+contract 3877`、探针 `371` 与静态检查沿用第 45 轮。教训：替换必须带断言（本轮两次静默失败）、清空状态要放在事件/回调而非 effect 体（`react-hooks/set-state-in-effect`）、请求断言需 `vi.fn<typeof globalThis.fetch>`。未完成：渲染单元 UI 与局部重做。
40. 渲染单元列表与局部重做（C07）：先读服务端语义再写界面——`RenderUnitPatch` 必填 `expected_revision`、空改动 422、相同依赖是**幂等空操作**（修订不变）、真实改动才 `revision+1` 且 `status=stale` 并清空产出，因此"重做"不等于"重新生成"。客户端增 `updateRenderUnit`（断言同修订或 +1 且 stale，并按改动回显），新增 `RenderUnitPanel`（就地改文本/声音 pin、带用户所见修订提交、空改动本地拒绝、修订冲突如实提示且不自动重试 + 手动刷新），`BooksHost` 章节可选并挂载面板。证据：workbench 测试 **547 → 553**（+6）、admin 110、五门禁全绿；本轮仅 Web 变更，Python `unit+contract 3877`、探针 `371` 与静态检查沿用第 45 轮。教训：prettier 重排会令锚点失配，替换必须断言并回读；async mock 无 await 与去掉 async 的 TS 返回类型冲突，正确写法是同步箭头 + `Promise.resolve`。
41. 渲染单元与工作台自有副本的关联（C07，独立保存的最后一环）：`RenderUnit.saved_asset_id` 原为硬编码 `null`。先读副本语义后判断关联应**派生**而不可拷贝——副本属于**产物**（请求只带 artifact+receipt），单一事实源留在 `saved_assets`，读取以 `LEFT JOIN LATERAL` 取最新副本，删除后自然回到 `null` 不悬挂；`render_units.saved_asset_id` 列保持不写。变更：领域 `RenderSource.saved_asset_id`、两条读路径接入连接并投影 `saved_copy_id`、payload 如实回报。证据：真实 PG+MinIO+回环产物源的集成用例（写入已产出结果 → 真实保存副本 → 单元与列表均读回、不同 receipt 不匹配、他项目不泄漏），并经 `validate_response` 证明**契约接受非空值**；**6/6 变异被杀死**（其中一处先 SURVIVED，补断言后杀死）；探针 **372**、unit+contract **3885**、ruff/format/mypy(507)/lint-imports 全绿。教训：核对基线必须用同一条命令（`--workbench-runtime`、`tests/unit tests/contract`），否则会把 348/2617 误读成回归。
42. 恢复：删除账本作为耐久证据（C08）。先确认语义边界——本项目的"恢复"是**快照恢复后重放删除账本**（`data-deletion.md`），不是撤销删除；账本缺失/不确定时必须**保持不可见并进入协调**。核对发现 `deletion_ledger` 只被状态查询和清理 worker 使用，**无任何读路径以其为准**，因此快照恢复会重新暴露已删除内容。实现：在原本就有 tombstone 闸门的 7 处读路径上并入账本证据（`_deleted`、`list_projects`、`list_transcripts` 的稿件与父项目两处、`list_books`、`list_chapters`、`list_units`），以账本元素 `[owner,kind,id,revision]` 的 `e->>1/e->>2` 匹配。证据：3 项**恢复演练**（真实 PG + 真实清理 worker：恢复行、删除 tombstone、账本留存）覆盖项目级/稿件级/单元级；**7/7 变异被杀死**且逐处对应；探针 **375**、unit+contract **3885**、ruff/format(666)/mypy(507)/lint-imports 全绿。主动收窄：minutes 队列闸门写好又撤回（缺自身演练用例，不做未演练的行为变更）；会议读路径尚无任何删除可见性闸门，与账本重放一并列为下一步。
43. 队列闸门与会议演练（C08）：把第 49 轮主动撤回的 `queued_ids` 闸门**连同演练**加回。演练按 project/transcript 两参数（真实 PG+清理 worker）：任务先可认领 → 真实删除+清理写账本 → 不可认领 → 快照恢复行并删 tombstone → **仍不可认领**。会议演练证明删除项目的会议在恢复后仍 404。两处**自我更正**：① 第 49 轮"会议完全没有闸门"的说法错误——`_locked` 的 `_deleted("meeting")` 与 `_source`→`get_project` 早已存在，上一轮的项目账本闸门已同时保护会议，本轮补齐正向证据；② 队列闸门的 project 半支**变异存活**（级联账本必含 transcript，构造不出该状态），保留为纵深防御并标注为未证伪。演练基础设施修正：`restore_rows` 加 `ON CONFLICT DO NOTHING`；按列 type_code(114/3802) 用 `Jsonb` 重新适配 jsonb。证据：探针 **378**（375+3）、unit+contract **3885**、ruff/format(666)/mypy(507)/lint-imports 全绿；恢复闸门累计 11 项变异（9 杀死、2 防御性未证伪）。
44. 音色切片第一步：C 侧目录契约与领域（契约先行）。核对确认权威语义在 A 的公开语音契约（`voice-resource.schema.json` + `/v1/nexus/voices|voice-designs|voice-clones|voice-drafts`），C 只消费不重写。**两个决定性发现**：① **A 的语音运行时尚未实现这些路由**（`speech/` 内无 `listVoices`/`nexus/voices` 引用，无测试），故 C 侧"用目录校验音色 pin"**本轮刻意不启用**，否则真实部署会直接失败；② **A 的语音报文体在 C 侧没有生成绑定**（`parse("VoiceSummary", …)` 实测 Unknown file contract），适配器须结构化映射进 C 已验证的领域值，或请 A 纳入其绑定面（记为 A 协调项）。交付：契约新增 `listVoices`/`getVoiceRevision` 2 个只读操作 + `VoiceList/VoiceSummary/VoiceRevision` 3 schema + `VoiceIdPath/LanguageQuery/VoiceCursor` 3 参数（游标明确为 A 的不透明值）；`bindings.json` workbench 面登记 2 路径 + 3 schema 名；`make contracts-generate` 生成、`contracts-check` 通过（28 文件），`x-nexus-http-operations` 38→40；领域 `domain/voices.py`（A 枚举原样、生命周期一致性、时区强制）+ 应用 `application/voices.py`（只读端口、越界入参不触达上游、稳定 503 错误）。证据：单元 **54 项**、contract **1715**、unit+contract **3953**、探测 378 不变、ruff/format(669)/mypy(510)/lint-imports/drift 全绿；**全部为合成/契约级证据，无真实 A 服务或真实音色验证**。**契约正例校验抓出一处真实设计错误**：修订号是共享 `Revision` = 十进制字符串 `^[1-9][0-9]{0,18}$`（非整数），第一版写成 int 被契约样例判不合格，已字符串贯穿领域/应用/测试/夹具；同时修正 `test_workbench_http_bindings` 里"每个操作都要有请求样例"的隐含假设（只读 GET 无请求体），冻结路径计数 30→32。下一步：适配器 + 路由与可用性门（未配置目录时 503）+ 渲染单元 pin 校验（等 A 运行时）+ 试听/发布编排。
45. 音色目录只读通路打通（C08/音色切片）：`public_voice_catalogue.py` 结构化映射 A 公开答复（`after`→A 的 `cursor`；只收 JSON、不跟随重定向、流式上限 256KiB；404→NotFound，其它非 200/非法 JSON/领域拒绝→`VoiceCatalogueUnavailableError`，**上游矛盾不得冒充 C 的 4xx**）+ `voice_views.py` 逐字投影 + `http_voices.py` 两个生成操作 + 组合根接线。沿用删除先例：**契约冻结的操作始终挂载**，未配置 speech base 时给稳定 `503 voice_catalogue_unavailable`（新增错误码），而不是消失成 404；status 的 `capabilities` 本就为空，无需新增能力条目。操作面计数断言随之更新（meetings 18→20、teleprompter 15→17、management 13→15）。证据：新增适配器 24 项 + 路由 13 项，unit+contract **3990**、探测 **378 不变**、format 674、mypy 515、lint-imports、contracts-check 全绿；仍为**合成/契约级**（A 运行时未实现 `/v1/nexus/voices`，无真实 A/音色证据）。下一步：UI 音色选择、pin 校验（等 A）、drafts/publications 编排、请 A 绑定语音 schema。
46. 工作台音色目录只读 UI（C08/音色切片）：`WorkbenchClient.voices()` 走 C 的 `/v1/workbench/voices`（同源、`redirect:"error"`、`no-store`），新增不变量“同页 `voice_id@revision` 唯一，同声线不同修订合法”；`RenderUnitPanel` 增加「已发布音色」选择器，选中即写入该单元 pin，重做仍按用户所见修订提交。**目录不可用明确报缺**（503→“音色目录不可用…”，只留“当前固定”一项，绝不用虚构音色填充），符合能力状态与“未实现能力不得虚构”约定。记录一致性候选：创建界面直连 A 协议而 pin 选择走工作台协议。证据：workbench **556**（+3）、admin 110、contracts 1246（Web 合计 **1912**），format/lint（含设计 Token 门禁）/typecheck/test/build 全绿；后端未改动，沿用第 52 轮 3990 与探测 378。未验证：无真实 A 音色服务、未跑真实浏览器/设备、pin 服务端校验未启用。
47. 独立保存的产品入口 + 已保存副本回显（C08/独立保存切片）：`WorkbenchClient.saveProjectAsset` 走 `POST /v1/workbench/projects/{project_id}/saved-assets`（**声明 201**；身份校验含 project/object 与来源对逐字一致，跨项目副本拒绝）；`TtsTaskResult` 增加目标项目选择器（有音频结果才懒加载 `projects()`）与「保存到工作台」，未知结果**复用同一幂等键**、成功后清空，失败区分「被拒绝」与「结果未确认」，项目列表缺席则禁用并明示不可用（不虚构目标/副本）；窄接口 `WorkbenchSaveClient` 使 file-workflow 不 import workbench 切片，`App.tsx` 注入同一 same-origin 客户端；渲染单元回显「已保存副本 <id>」/「尚无已保存副本」。**契约发现**：`RenderUnit` 带 `saved_asset_id` 时必须同时带 `source_artifact_id`+`source_receipt_id`（单字段 fixture 被 schema 拒绝，证明约束真实生效）。证据：workbench **562**（+6）、admin 110、contracts 1246（Web 合计 **1918**），五门禁全绿；后端未改（沿用 3990/378）。未验证：真实 A 制品服务与非 JSON 绑定仍缺，真实复制闭环未跑；未跑真实浏览器/设备；转写产物保存入口未做；`render_units.saved_asset_id` 列取舍待 A 对齐。
48. 完成稳态删除清扫：配置清理身份时 lifespan 启动有界后台清扫（默认 5s，`cleanup_interval_seconds` 1..300s），关闭时取消并等待任务结束；单次清扫失败记录日志后继续（账本仍记录欠账，下次 tick 或下次启动重放）。证据：新增真实 HTTP 集成用例——运行期间受理的删除在**不重启**的情况下被清扫结清，且关闭后新受理的删除保持欠账（证明任务确实停止）；变异「不启动清扫」与「泄漏清扫任务」均被杀死。未完成：生产清理 DSN 注入与部署顺序仍待 A 协调；渲染单元、导出、助手、音色等切片未做。

下一执行顺序：

13. 继续独立保存/导出与业务恢复，以及音色候选/校验/发布、会议实时接线、助手等剩余切片；按有效契约和可用依赖推进，不能以领域规则或页面完成代替完整业务闭环。
14. 在 A 协调的共同 SHA、生产迁移/部署及 A/B 确定版本就绪后，完成公开 speech/admin 的真实闭环；分别取得内容网络、真实浏览器/设备、Linux 后端和 GPU 联调证据。每轮交付标明范围、版本、测试、未验证项和依赖 owner。

本次证据入口为[独立入口计划](2026-10-24-team-c-runtime-root-plan.md)、[独立入口交接](2026-10-24-team-c-runtime-root-handoff.md)及[真实 Workspace 联验](2026-10-24-team-c-workspaces-live-handoff.md)。独立入口交接中的预审结果属于修复前记录，后续交付须更新，不据此宣布完整 Goal 完成。

[C00 文本字符边界交接](2026-10-24-team-c-c00-text-handoff.md)补充项目/稿件的内容拒绝与失败重试证据。
[C02 文件任务跨刷新恢复交接](2026-10-24-team-c-c02-recovery-handoff.md)补充创建/取消原键、事件水位和浏览器刷新证据；生产入口仍待接入，上传阶段恢复见后续交接。
[C02 文本配音交接](2026-10-24-team-c-c02-tts-handoff.md)补充固定文本素材与声音/参数版本、合成音频试听下载及 TTS 刷新证据。
[C02 上传阶段刷新恢复交接](2026-10-24-team-c-c02-upload-recovery-handoff.md)补充原上传键、输入身份、素材确认与 Job 原子交接；生产入口和真实服务联调仍待接入。
[C02 固定版本文本导出交接](2026-10-24-team-c-c02-text-export-handoff.md)补充已保存原文/校对版本的 UTF-8 输出与删除屏障；公开页面和字幕协议仍待接入。
[C02 文本下载契约候选](2026-10-24-team-c-c02-download-contract-handoff.md)提供可执行正负样例和 A/C 冻结输入，状态仍 proposed，未接生产。
[C01 任务事件与正式入口](2026-10-24-team-c-c01-events-handoff.md)补充同源公开 API 消费、水位/历史过期与生产 bundle 合成 HTTP/SSE；真实部署及跨刷新恢复仍待完成。

[C01 Admin 同标签页恢复](2026-10-24-team-c-c01-recovery-handoff.md)补充生产入口的原取消键/目标与水位跨刷新恢复、存储故障阻断；真实部署联调仍待完成。

[C02 素材 HTTP 原身份恢复](2026-10-24-team-c-c02-asset-reconcile-handoff.md)补充 A 上传 helper、Asset Location、v5/v6 hint 与原素材跨刷新只读核对；正式 Workbench 宿主和真实服务仍待接入。

[C02 正式文件转写入口](2026-10-24-team-c-c02-host-handoff.md)补充正式生产宿主、原状态恢复/事件/取消及原始 JSON 下载的真实 Chromium 合成 HTTP 证据；模型发现、校对/导出和真实服务联调继续推进；正式配音见下一单元。

[C02 正式文本配音入口](2026-10-24-team-c-c02-tts-host-handoff.md)补充正式同源 TTS 宿主、固定声音/参数版本、原文本/hint 刷新恢复、WAV 试听下载与切换释放的真实 Chromium 合成 HTTP 证据；发现/资格和真实服务联调仍未完成。

[C05 固定稿件封存](2026-10-24-team-c-c05-seal-handoff.md)补充不可变 sealed revision、固定源读取及 owner-only v2/真实 PG 重放与删除证据；尚未注册公开会议路由，不把基础用例当完整 C05/C06 完成。

[C05 提词跟随与手动阅读](2026-10-24-team-c-c05-prompter-handoff.md)补充固定本地稿件、有限窗口/旧epoch隔离核心、正式手动阅读与显式浏览器恢复；公开实时接线、服务端提词持久化和真实语音跟随仍待完成。

[服务端提词持久化](2026-10-24-team-c-teleprompter-persistence-handoff.md)接入两项已声明提词操作的独立 PG 事务、旧稿 revision 拒绝与原键重放、tombstone/cleanup 级联及最小权限；公开实时接线与真实语音跟随仍待完成。

[C06 内网 LLM 与纪要候选](2026-10-24-team-c-c06-llm-handoff.md)补充默认关闭的固定模型/模板调用、有限响应与取消、sealed 当前源重检及真实 PG 删除屏障；持久 MinutesJob、公开路由/页面、助手 turn 和真实内容网络/混合负载资格继续推进。

[C06 持久纪要任务](2026-10-24-team-c-c06-tasks-handoff.md)补充固定源/profile/binding、原意图任务/attempt、提交未知恢复及同事务发布屏障；公开 Meeting/Minutes 接线、停止对账和真实资格仍待完成。

[C07 固定依赖与局部失效](2026-10-24-team-c-c07-render-handoff.md)补充固定稿件/声音/模型等版本、显式生成代次、A→B→A拒绝复活和仅目标单元/角色失效；领域源引用不授予保存/导出资格，真实持久化、公开接线、独立副本/质量确认/导出与实际资格继续推进。

## 历史首轮与接力

以下为当时的接力安排，供追溯，不再自动执行。

[a98b2bb 集成核验](2026-10-24-team-c-a98-integration-handoff.md)记录本地已吸收 #65–#68 且保留全部团队 C 改动；该条保留原时点事实，历史单元基线保持原记录。

[2874c839集成核验](2026-10-24-team-c-2874-integration-handoff.md)保留该时点事实：保留191文件交付，吸收#69–#71，原样unit/contract1659、隔离探针262、Web530及四条正式Chromium合成流程通过，旧网关时钟失败解除。

[7372778集成与C1 JSON消费](2026-10-24-team-c-c00-c1-handoff.md)保留该时点事实：保留198文件交付，吸收#72–#74，消费项目/稿件JSON生成入口；原样1902、PG262、Web584及四条正式Chromium合成流程通过。

[e0d8992集成与C1 HTTP接入](2026-10-24-team-c-c00-http-handoff.md)保留该时点事实：保留204旧C文件及双方README，吸收#75/#76，补权威错误状态并通过既有生成流水线接入七个项目/稿件HTTP操作；原样2205、PG265（含真实loopback HTTP）、Web815通过。一次终审的默认docs公网CDN问题经TDD修复并全库回归，405 Allow的Minor转入下一单元。

[3b8052集成与C1管理HTTP](2026-10-24-team-c-c1-management-handoff.md)保留该时点事实：安全吸收#78代码而未运行VAD，保留旧217文件及双方README。C-owned组合工厂覆盖C1全部13项操作，新增作用域绑定分页、真实配置状态、项目/稿件删除预检/确认/清理查询及完整405 Allow；Python2414、PG269（真实loopback）、Web891通过。

[项目与稿件版本工作台](2026-10-24-team-c-c1-editor-handoff.md)保留该时点事实：沿3b8052消费生成C1入口，实现项目/稿件发现创建、精确历史与追加校对、sessionStorage草稿/原键恢复、显式冲突继续及存储失败屏障；53项针对性、Web940、4个实际Chromium合成HTTP上下文通过。一轮终审两项Important经失败测试修复，累计237文件交付；Python/PG复用上一单元未变证据。

[7cd18879集成](2026-10-24-team-c-7cd-integration-handoff.md)保留该历史单元事实，当时本地base为该SHA：安全吸收#77/#79的Meeting/Minutes及公开realtime生成入口，连同#80文件媒体准备与#81租约隔离代码；237旧C文件中232字节不变，5个交集保留双方语义，生成物由A现行流水线重建，357条HTTP样例。当时Python2735、隔离PG269、Web1150及Chromium合成流程通过，累计238文件。C2/C3解析依赖在当时交付，后续Meeting/Minutes handlers、页面及实时消费者见较新交接；共享生产root/迁移、Artifact导入/下载、独立副本/恢复、设备/Linux/GPU及完整真实资格继续推进，Goal/Gate未关闭。


[Meeting/Minutes HTTP](2026-10-24-team-c-meeting-http-handoff.md)接通当前五项持久业务：Meeting创建/封存/读取与Minutes入队/读取；原键返回原接受快照，GET返回当前任务。Meeting依赖纳入项目/稿件删除预检、完整确认与清理，未知LLM执行仍阻断清理。C工厂显式装配18项，默认13项；会议页面/实时采集、save_audio保存、真实LLM、生产root/迁移及完整C00–C08资格继续，当时 Goal 文档为 active；现已转为 superseded。

1. 核对当前共同 `integration_base_sha`、工作区改动和各队具名制品；使用短分支，公共文件按维护者串行集成。暂以本地基线开展的独立单元，须在联合联调前对齐总指挥指定 SHA。
2. 从 C00 #35 起步：消费 A00 发布的边界类型/客户端/事件 validator；完成项目和稿件版本、独立存储边界、开发 fake 及错误场景。生成器未交付时推进不依赖 DTO 的领域规则与界面状态，不手写替代生成产物。
3. 推进 C01/C02 的最小 fake 业务切片：能力/任务视图、上传→任务详情→校对→下载，覆盖取消未知、失败/版本冲突、重放、SSE 重连、历史和 Artifact 过期。
4. C04 先用合成 PCM/fake WS 验证有界缓冲、时钟、credit/gap/epoch、播放 generation、停止与卸载清理；真实设备与远端联调分别取证。
5. 接入 A02/A04/B03 的确定版本，完成真实文件闭环；依照依赖表推进 C03/C04，再推进 C05/C06/C07。有声书不等待助手，普通会议不依赖 LLM。C08 的浏览器、内容生命周期和恢复检查持续开展。

## 所有权与执行边界

- C 主责 workbench 契约、业务版本/导出/LLM 语义及 Web Token/共享组件；工作台内容保存在自己的 schema、对象区和生命周期中。
- 工作台只使用公开语音协议，不导入 speech 内部模块、不共写底座表、不直接调用引擎。生成流水线和公共 speech/admin 契约归 A，执行/GPU/模型资格归 B。
- C 提供模块迁移及用例，A 协调迁移框架、顺序、组合根、共享 CI/锁文件和 deploy 集成；同一文件只有一个写入者。
- LLM 默认关闭，只接受部署批准的服务端内网端点，禁止公有云回退。内容及派生内容不进入日志或部署网络之外。
- Linux 后端/镜像、合成媒体、真实 Mac 浏览器/设备、GPU 和跨主机结果分别记录；不得扩大证据的适用范围。
- 提交、推送、外部消息和运行态变更遵守当前明确授权。缺少这些授权不妨碍独立编码与本地验证，交接可使用包含新增文件的补丁包。

## 完成与阻塞条件

C00–C08 全部约定范围、生产消费者及必要真实验证均有可追溯证据，才可完成团队 C 目标。fake 通过、页面完成、代码合并不能代替生产闭环和真实浏览器验收。A 汇总集成证据，用户决定 G0–G5 放行；C 不代替 A/B 宣称其专业资格通过。

每轮交接记录 `base_sha`/`head_sha` 或补丁与校验值、契约版本、修改范围、命令/环境/结果、未验证项、阻塞 owner/解除条件及下一接入动作。依赖缺失时继续独立任务，下游明确保持待联调；不猜测字段、不绕过协议，也不重复无新证据的检查。

[C00 版本单元交接](2026-10-24-team-c-c00-version-handoff.md) · [C00 生成请求消费交接](2026-10-24-team-c-c00-project-boundary-handoff.md) · [C00 元数据分页交接](2026-10-24-team-c-c00-listing-handoff.md) · [C00 PostgreSQL 事务交接](2026-10-24-team-c-c00-postgres-handoff.md) · [C00 删除事务交接](2026-10-24-team-c-c00-deletion-handoff.md) · [C01 Admin 任务中心交接](2026-10-24-team-c-c01-task-center-handoff.md) · [C02 文件任务与 A00 消费交接](2026-10-24-team-c-c02-file-flow-handoff.md) · [C02 原始转写导入交接](2026-10-24-team-c-c02-transcript-import-handoff.md) · [C04 合成播放单元交接](2026-10-24-team-c-c04-media-handoff.md) · [C04 浏览器采集交接](2026-10-24-team-c-c04-capture-handoff.md) · [跟踪矩阵](2026-10-23-implementation-tracking.md)

当前分段交付：[会议记录与纪要工作台](2026-10-24-team-c-meeting-ui-handoff.md)提供来源绑定、封存与原键恢复的真实浏览器/隔离PG证据；C00–C08及全部真实资格仍按上述退出条件保持进行中。

当前依赖基线：[eb299f7集成交接](2026-10-24-team-c-eb299-integration-handoff.md)，在[9eea859历史集成](2026-10-24-team-c-9ee-integration-handoff.md)之后保留C全部业务并吸收已合并#85–#88；Python3314、Web1471、隔离PG及业务浏览器296与ASR/TTS合成浏览器回归通过。[删除预检、确认与恢复页面](2026-10-24-team-c-deletion-ui-handoff.md)已提供正式消费者、原键与当前清理事实的浏览器/私有PG证据。[实时ASR消费与字幕页面](2026-10-24-team-c-realtime-asr-handoff.md)补充生成事件状态机、credit/PCM、原键与最终正文草稿、断线gap及生产bundle合成REST/WS/麦克风证据。公开生产realtime、双工、会议后续流程、独立副本/恢复和其余工作包继续，完整目标未完成。

[实时TTS消费与播放](2026-10-24-team-c-realtime-tts-handoff.md)补充固定引用、文本冻结与有界Worklet播放，分别记录生成/网络/渲染及合成浏览器验证；真实服务/模型/物理设备与双工继续，完整目标未完成。

[音色设计与克隆创建任务](2026-10-24-team-c-voice-create-handoff.md)接入正式双模式输入、已有素材用途确认、生成Job/Asset消费者和原键恢复、首次明确拒绝纠正及原operation显式恢复；C03首个创建单元已交付，候选/再生成/试听/校验/发布、声音复用与真实A06/B05资格继续。

**更正（第 77 轮）**：上面"6 个孤儿探针"的计数是**错的**——第 76 轮的扫描把 `.runtime/` 里的引用也算作"有人运行"，真实数字是 **15 个**（漏掉 9 个）。同轮又判定并接入 `admin-workspaces-probe`（与已在门禁的 live 版测不同路径：合成 HTTP vs A 的临时夹具），并为这条线索立了**永久守卫** `tests/integration/test_probe_inventory.py`：C 侧探针要么被门禁运行，要么必须出现在显式欠账清单里并写明域与原因，清单过期即失败。剩余 9 个欠账（5 个不同路径 + 4 个疑似被取代）已记录在案。同时核实：**credit 耗尽与迟到音频已被现有证据覆盖**（ASR 探针的 `credit_exhaustion_stopped` 与三份 web 单测），故不另写重复探针。门禁 **410**。

**第 78 轮把 9 个欠账逐个实跑分类**：6 个通过并接入（`workbench-tts`、`workbench-asset-reconcile`、`workbench-file-host`、`workbench-recovery`、`admin-task-events`、`admin-task-recovery`），3 个实测**失败（陈旧）**并登记其失败信息（`workbench-tts-host` 命中未建模路由、`workbench-manuscripts` 找不到元素、`workbench-prompter` 区域内出现两个 status 元素）。机械字段差集推翻了我的"疑似被取代"判断——`workbench-tts-probe` 独有 **原生 WAV 解码**与**卸载时撤销 blob URL**，是漏接入而非被取代。新接入用例钉住 `historyExpiredReread`、`acknowledgementSaveFailureUnknown`、`downloadBytesVerified`、`qualifiesLiveSpeechOrGpu=false`、`unknownUploadRecovered=false`、`productionUI=false` 等诚实性质。

**第 79 轮把"待人工判断"变成判定**：读代码确认提词页的 `提词阅读` 区域里两个 `role="status"` 含义不同（本地操作状态 / 部署操作状态），**不是重复播报缺陷**；但第三个活区域（部署内已保存快照）**缺可访问名称**——已补 `aria-label="部署提词快照"`，并加单测固化"两个活区域各自可按名称寻址"（web **597**）。据此把陈旧探针的断言从不精确的 `getByRole("status")` 改为指名区域，**修复后实跑通过并接入门禁**（`microphone_calls=0`、`api_requests=0`、`qualification=manual-browser-reading-only`），陈旧清单从 3 项减到 2 项。

**第 81 轮**：两个陈旧探针各自定位到根因。`workbench-tts-host-probe` 修好并接入门禁——先把错误消息改成带方法与路径，一次运行即锁定 `GET /v1/workbench/projects`（页面后来会读项目目录，旧合成服务未建模），按 workbench 契约的 `listProjects` 响应样例补上路由；修复后实跑通过并钉住 `late_content_discarded`、`native_wav_preview_and_download`、`switch_revokes_url`、`real_backend_or_model_qualification=false` 等。`workbench-manuscripts-probe` 只做到部分修复（流程改为先核对目录再选稿件），失败点前移到"`核对当前版本` 禁用"；据 `locked = busy || pending`（`web/workbench/src/manuscripts/ManuscriptHost.tsx:59`）记录精确诊断，并因**已在门禁的真实运行时探针在同一按钮上成功**（`workbench-manuscripts-real-probe.mjs:109`）而不记为产品缺陷。陈旧清单 **3 → 1**。

**第 82 轮**收尾最后一个陈旧探针的定位：让探针自己产出证据——失败点 dump 显示应用停在它文档化的"结果未定态"（`按原键重放` 未自动发送，**不把未知当成功**，行为正确）；按应用意图补上重放后仍不回恢复，且探针的 `catch {}` 原本吞掉真实原因、只在成功路径输出诊断——已改为保留真实消息并在失败时打印已尝试变更与服务端处理器错误。证据排除了两类原因（`handler errors` 为空 = 非服务端拒绝；同键两次 = 确有重放），但根因仍未解释，**不记为缺陷也不记为通过**；陈旧欠账保持 1 项并附精确诊断。

[五个孤儿探针接入门禁](2026-10-25-team-c-orphan-probes-gated-handoff.md)把上一轮的更正做成一次**扫描**：对 `scripts/dev-env/` 每个探针统计除自身外的引用数，C 侧有 **6 个探针无人运行**（capture / media / voice-create / admin-task-center / file-demo / admin-workspaces）。前 5 个已接入门禁并钉住各自**自曝的边界**（`physicalMicrophoneAccessed=false`、`autoplayPolicyBypassedForProbe=true`、`acceptanceNotRelease=true`、`productionScenarioExcluded=true`、`cleanupNotAssumed=true`、三个 live 声明一律必须为 false）；`admin-workspaces-probe.mjs` 暂不接入，因其与已在门禁的 live 版关系需先判定。门禁 **406**。

**更正（第 75 轮）**：上一轮"实时字幕页"交接里"C04 界面此前没有任何真实 Chromium 取证"**不准确**——`scripts/dev-env/` 里早已存在两份自包含合成实时探针（ASR 2026-10-24、TTS 2026-10-24），但它们**没有任何测试包装、不在任何门禁里**，因此准确说法是"没有进入门禁的可追溯证据"。本轮把它们接入门禁（新增 2 个用例：ASR 3 个合成会话/10 次已确认写入；TTS 收到 48000 样本并渲染 48000 无丢失），详见[合成实时探针接入门禁](2026-10-25-team-c-realtime-evidence-promotion-handoff.md)。门禁探针 **401**。

[实时字幕页的真实浏览器取证](2026-10-25-team-c-realtime-consent-browser-handoff.md)补上 C04 界面此前缺失的证据：该页承诺"先创建并连接…**再由你明确开启麦克风**"，本轮用生产包 + 真实组合根（无实时底座）并把 `getUserMedia` 换成计数器——断言的是**零次请求**：打开页面 0 次、请求会话被公开拒绝（`/v1/nexus/realtime/sessions` → 404）之后**仍然 0 次**，且未连接会话时**根本不提供**"开始麦克风采集"控件、创建表单可用键盘按顺序到达、360px 无横向滚动、无非本源出站与页面异常。真实浏览器用例增至 **7 份**，探针 **399**。

[书籍页表单的真实键盘与窄屏核查](2026-10-25-team-c-keyboard-narrow-verification-handoff.md)把 C 侧可达的真实验证推深一层：新增表单此前只有鼠标/程序化填入的证据。核查先暴露真实缺陷——成功后清空输入会让提交按钮变禁用，**键盘焦点掉回 document**；已改为成功后把焦点移回该表单第一个字段。真实浏览器里用**只用键盘**的流程创建单元（7 个字段逐一 Tab 并断言焦点落到下一个具名控件、Enter 提交、焦点回落），并断言 360px 无横向滚动。workbench 单元 **596**。

[音色单修订核对](2026-10-25-team-c-voice-revision-consumer-handoff.md)关掉清点名单的最后一条，并给出**清点结论**：收紧到"方法 + 路径 + 排除测试文件"后，**已绑定操作的生产消费者缺口归零**（剩余 4 条是 A 未绑定的操作，不在 C 清点范围内）。单修订是唯一带 `retired_at`/`disabled_reason`/`model_binding` 的读取——只看目录摘要，界面无法说明一个被固定引用的音色为何不可用。真实浏览器侧只能证明**拒绝路径**（任何 C 可达的真实部署都没有语音底座：目录与单修订都是 503），成功路径待 A 的音色目录。workbench 单元 **594**。

[项目改名（元数据版本 CAS）](2026-10-25-team-c-project-rename-cas-handoff.md)补上 `updateProject` 这一真实缺口，并修掉一处诚实性问题：`BooksHost` 的错误映射此前没有 `revision_conflict` 分支，**明确的版本冲突被报成"结果未确认"**，会诱导盲目重试；现改为冲突文案且不自动重试。落点刻意避开 manuscripts 的持久恢复日志（加字段会改变日志格式，可能让在飞的未保存草稿判为不可恢复）。真实浏览器证据含**带外推进修订后界面用旧修订改名**的 409 场景：冲突文案出现、被拒名字不显示，HTTP 复核部署里存的是带外注入的名字、`metadata_revision=3`（旧修订没有赢）。workbench 单元 **591**。

[渲染单元创建补线](2026-10-25-team-c-render-unit-create-consumer-handoff.md)把清点再收紧一格（**排除测试文件的调用**，6 条），并因此划掉一个"假缺口"：`getTranscriptRevision` 并非缺陷——`getTranscript` 带 `revision` query，界面的"读取固定版本"走的就是它，既有真实探针已断言历史原文可还原。真正的洞是 `createRenderUnit`（章节能建、单元不能建 → 有声书产不出任何东西）：本轮补齐客户端与表单（显式 pin 可登记，目录可用时给出下拉），并由真实浏览器证据闭环——同一生产包流程建书→建章节→建单元，再由真实 HTTP 复读单元逐字段相等、`status=pending`（**登记≠生成**，生成仍是 A 的 `generateRenderUnit`）。workbench 单元 **588**。

[有声书章节创建补线](2026-10-25-team-c-chapter-create-consumer-handoff.md)先纠正我自己的错误结论：第 66/67 轮"只剩 3 条无消费者"是**方法错误**（只比路径、不分方法），按"方法+路径"重做真实为 **7 条**。其中 `createChapter` 是真实功能洞——书能开、章能列，却无法新建章节；本轮补齐（客户端断言 `book_id` 一致、失败不轮换请求键、顺带修正书籍创建每次现取新键与"按原键重试"提示矛盾的问题），并由真实浏览器证据闭环（生产包建书→建章节，再由真实 HTTP 复读同一章节，`GET /chapters/{id}` 与列表逐字段相等）。探针 **398 通过**（397+1），workbench 单元 **585**。

[已保存副本回读的生产消费者](2026-10-25-team-c-saved-asset-read-back-handoff.md)是机械比对的结果：44 条已冻结操作里只剩 3 条没有 Web 消费者（`getSavedAsset`/`getTranscriptRevision`/`getVoiceRevision`），本轮补上 ROI-1 点名的 `getSavedAsset`——按 id 显式回读、展示服务端自己的媒体类型/字节数/sha256/来源，404 与未确认分别如实报告；真实 PG+MinIO 集成与**真实 HTTP**（真实组合根）双双证明回读不再向底座取产物、未知 id 与另一 workspace 均为 not-found；探针 **397 通过**（395+2，含新增读路径用例）；workbench 单元 **582**。一处预期修正：未配置对象区时该部署不挂载副本路由、返回 404 而非 503，已按实测改断言。

[九份 issue 正文按当前现实整理](2026-10-25-team-c-issue-four-part-refresh-handoff.md)解决的是**issue 与工作区脱节**：九份工作包正文停留在开工计划（0 个已勾选子项、0 条证据），而工作区已有十个冻结单元与五份真实浏览器制品。整理后正文保持四段（目标/进展总结/任务清单/AC）、无平行章节与"现实核对"块；进展总结写清已交付、未集成与阻塞 owner+解除条件；**逐轮命令、计数、制品路径与补丁 SHA 全部移入评论**；只勾选 8 个有单独证据的步骤，**41 条 AC 一律未勾**。逐字回读制品：`.runtime/team-c/2026-10-25-issue-four-part-readback.json`。

[部署内提词状态的生产消费者](2026-10-25-team-c-teleprompter-state-consumer-handoff.md)补上两处缺口的"消费者"一端：`getTeleprompterState`/`updateTeleprompterState` 后端早已接入独立 PG，但 Web 侧**没有任何消费者**（面板只做本地阅读，文案还写着"不保存到服务端"）。新增同源读取（404=尚未保存，**绝不当作位置 0**）、显式写入（修订+位置+手动接管+请求键）、冲突保留上次读取且不重试；真实浏览器 + 真实 HTTP 复核证明"更旧修订被 409 拒绝且没有移动位置"。探针 **394 通过**（393+1）、workbench 单元 **580**。

[Admin 部署诊断页](2026-10-25-team-c-admin-diagnostics-handoff.md)把可读事实接到管理入口：AdminUI 此前只有三张写明"计划"的静态能力卡，**没有任何一处读部署自身状态**。现在同源只读 `GET /v1/workbench/status`（生成契约校验），**打开页面不发请求**、点击"读取部署状态"才读；逐条渲染服务端五个能力（状态原样）、内网模型绑定、本地化的共享部署声明（契约里是固定代码 `shared_deployment`，界面不得回显英文），并明确列出尚未接入的队列深度/预算/租约/outbox/计量/GPU；读取失败只报错、不渲染能力。证据：Admin 单元 110→**118**、真实 Chromium+生产 Admin 包+真实组合根+隔离 PG 两场景；探针 **393 通过**（原 391 + 2）。

[助手诚实态的真实浏览器资格](2026-10-25-team-c-assistant-honest-state-browser-handoff.md)把上一单元的 jsdom 结论搬到真实环境：生产包 + 真实组合根 + 隔离 PG 下，默认部署（`disabled`）与"已配置内网模型但无语音底座"（`unavailable`）两种判定都被真实 Chromium 确认——诚实文案可见、**创建表单根本不存在**、`/assistant-turns` 请求为 0、重载后结论不变、出站与页面错误均为 0。这两条用例已进入 `--browser` 门禁（探针 391 通过）。

[语音助手诚实消费面](2026-10-25-team-c-assistant-honest-consumer-handoff.md)在工作台里补上了此前**完全不存在**的助手界面（连导航项都没有）。消费面严格以服务端能力三态决定能否提交：未启用或缺依赖时创建表单根本不出现，可用时才显示项目/已发布音色/文本/请求键；回合按契约断言（失败必须有 error、非失败必须 error=null、成功必须有 speech_job_id、携带 job 就必须带非空文本），失败显示服务端错误码且不自动重试。本轮证据是 jsdom 单元（会话 7 项 + 宿主 4 项，全量 Web 574 通过）与门禁，**真实浏览器下的两态呈现仍待下一轮**。

[工作台状态上报真实的部署能力三态](2026-10-25-team-c-capability-report-handoff.md)修掉了诚实 UI 的前提缺口：契约要求 `capabilities` 上报 transcription / meeting_minutes / assistant / teleprompter / audiobook_export 的 `available|disabled|unavailable`，实现却恒返回空数组，消费端只能靠猜。现在状态由装配事实推导（语音客户端、对象区、批准 LLM profile、导出 producer 与循环是否运行），隔离 PG 上钉住未配置部署、LLM+语音底座、导出三种情形的取值。

[导出产线在服务运行时中的预接线](2026-10-25-team-c-export-production-prewiring-handoff.md)补上了"队列有人认领"这一环：导出队列、认领、对象发布与结算都在工作台侧，但 producer 属语音生成边界，此前 `ExportWorker` 只在测试里被手工调用，生产组合根从不构造它。现在 `create_runtime_app(settings, *, export_producer=None)` 提供注入点，`export_interval_seconds` 默认 `0`（关闭），只有同时具备已配置 producer 与对象区时才启动派发循环；隔离 PG + 真实 MinIO 上证明运行时自有循环能认领并完成排队导出、默认关闭时不动队列、无 producer 时不认领。producer 仍为测试替身，真实字节来源待 A。

[稿件创建消费者缺陷修复与稿件真实浏览器资格](2026-10-25-team-c-authoring-consumer-fix-handoff.md)把写作面接进真实运行时资格时挖出一个**真实生产缺陷**：手工新建稿件在真实部署上永远卡在"提交结果尚未确认"、编辑器无法出现——契约与领域都规定手工稿的原始修订是 `draft`，而 Web 客户端无条件断言 `sealed`，契约 fixture 的示例也自相矛盾，两个 Web fixture 同样写错，因此单元测试一直通过。修复覆盖契约 fixture、客户端分支断言、界面文案与测试（新增缺陷用例）；配套新探针在真实 Chromium + 真实组合根 + 隔离 PG 上跑通"创建项目/原稿 → 核对当前版本 → 校对提交修订 → 读取历史版本 → 从历史保存 → 刷新保留草稿"，并如实记录既有合成稿件探针从未被调用、locator 已过期。

[真实运行时 × 真实本机模型 × 真实浏览器](2026-10-25-team-c-real-runtime-model-browser-handoff.md)把**配置的本机模型**接进同一条真实链路：页面上排队的纪要通过轮询确认由运行时自己的派发器认领并完成（测试不执行任何任务），读回 `status=succeeded`、`llm_model` 等于配置模型、正文非空，并且页面显示的就是模型实际返回的内容；过期与级联删除同样走真实 HTTP 与真实 sweeper。探针新增两个**默认关闭**的开关（保持既有用例逐字不变），模型文本以 `model-text.txt` 落盘便于复核。

[真实运行时 × 真实 Chromium 交付资格](2026-10-25-team-c-real-runtime-browser-handoff.md)把浏览器证据从"合成服务集 + 手工装配 ASGI"提升到部署自己的组合根：真实生产包、真实启动检查、真实删除账本重放、真实隔离 PG，且不手工驱动 worker——欠账清除改由**重启后启动期重放**完成，使 ADR 0005 成为可执行证据。同时记录两个真实发现：契约入口会拒绝静态资源（API 不是静态站点，部署须由前端层代理 `/v1`），以及真实 sweeper 的中间态会掩盖旧用例的手工清理假设。

[内网 LLM 接线与纪要派发 worker](2026-10-25-team-c-minutes-worker-handoff.md)在用户授权使用真实环境后补齐 C06 的启用态：设置面新增默认关闭的 LLM 绑定（仅本机或内网端点，公网主机在启动时被拒），组合根把同一绑定注入会议纪要与助手，并新增随 lifespan 启停的纪要派发 worker。真实证据：本机 oMLX 模型经真实 PG 完成"入队→派发→落库"，其中一条由真实 `create_runtime_app` lifespan 自己的循环完成，并核对 status 上报的 `llm_enabled`/`minutes_profile`；单元+契约 3997、隔离 PG 探针 379、纪要集成 32、live 2 全通过。同时记录环境发现：CLIProxyAPI 的 `omlx-*` 别名响应回上游原生模型名，不满足 C 的 pinned-model 校验，故部署配置直连本机 oMLX（仍不出机）；真实 A/B 服务、设备、Linux/GPU 与生产部署继续待联调。

[工作空间读取与依赖集成](2026-10-24-team-c-workspaces-read-handoff.md)吸收A #90–#93至2a46c6e，接入Admin同源生成GET列表/详情及显式分页；Workspace为共享分组，创建/修改/配额、A真实GET/PG和完整管理资格继续。
