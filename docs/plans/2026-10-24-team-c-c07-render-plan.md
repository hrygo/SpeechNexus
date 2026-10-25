# C07 固定render依赖与局部重做实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans 按既有授权 native inline / TDD 执行；源冻结后一次 fresh sol_worker high 终审。保留当前192文件成果，不提交、推送、启用服务或修改A共享入口。

**Goal:** 实现有声书render单元的不可变依赖、生成代次和局部stale规则，为后续真实持久化、保存副本与独立导出提供业务核心；不替代完整C07。

**Architecture:** domain/render_units.py维护固定源/模型/声音等依赖和单元状态；domain/book_render_plan.py维护同书单元集合的CAS与局部失效。内部对象不依赖框架、SQL、生成DTO或speech内部类型；所有I/O留给后续application/infrastructure。

**Tech Stack:** 现有Python3.14、frozen/slots dataclass、hashlib/json、pytest；不新增依赖。

**Spec:** [产品设计§9.3](../speechnexus-product-architecture.md#93-有声书)、[C07](2026-10-23-three-team-delivery-plan.md#43-c-团队应用与体验)、[工作台契约](../../contracts/workbench-openapi.yaml) RenderUnit/Generation与SavedAsset约束、[ADR0005](../decisions/0005-complete-execution-and-data-lifecycle-contracts.md)。沿用已授权范围和既定native执行方式，不新增产品决策。

## Global Constraints

- 每单元固定原稿/工作稿revision、章节/角色、voice_revision、pronunciation_revision、model_binding_revision、profile_revision与文本规划revision；正文UTF-8无损，拒绝NUL/未配对surrogate，render文本1..200000码点。
- 原始声学结果、独立副本和执行事实不因局部编辑删除；同一角色换声只影响引用该角色的单元，不能由全书casting版本递增推导全书失效。
- pending/generating/ready/stale/failed为既有单元状态；ready仅有固定源Artifact/receipt，不等于已保存、校验、确认或可导出。
- 完整C07还须实现TXT/章节/角色持久化、公共语音命令消费、独立副本校验/保存、独立质量/试听/匿名确认、有界export worker/检查点/原子发布、删除恢复和实际页面/模型资格。
- owner的公共生成/迁移/组合根不改；私有依赖摘要不是新的公开DTO或协议digest。未生成入口不以手抄wire形状填补。

## Review Focus

1. A→B→A编辑/换声不能自动复活旧结果，所有晚到代次/错误unit、Job或依赖的结果须拒绝。
2. 整书casting递增不能让无关单元失效；章节/角色重复和同角色混合声音需明确拒绝，而非悄悄重排/更改来源。
3. Unicode原样参与指纹；bool revision、非法时间、变更容器及错误类型不能绕过不可变状态/严格CAS。
4. ready不等于saved/exportable；没有实际内容校验或质量确认时不得构造真实资格，任何编辑不删除独立副本/审计事实。
5. 内部指纹/状态不能伪装公开机器契约、真实持久化、A/B执行或完整C07/Gate资格。

## Task 1：固定依赖与render状态

**Files:** Create `backend/src/speechnexus_backend/workbench/domain/render_units.py`; Test `tests/unit/backend/test_workbench_render_units.py`。

**Interfaces:** Consumes现有 `validate_revision_number`、`RevisionConflictError`；Produces `RevisionPin(id, revision)`、`RenderDependencies(original,working,chapter_id,role_id,text_plan,voice,model,model_binding,text,pronunciation=None,profile=None)`及派生`sha256`、`RenderSource(unit_id,job_id,artifact_id,receipt_id,dependencies_sha256)`、`RenderUnit.create/start/revise/complete/fail`。

- [x] 写测试并观察缺模块RED：固定全部参数/UTF-8指纹、无正文repr、pending→generating→ready和failed。
- [x] 实现不可变对象；`start(expected_revision,job_id,generation_id,now)`显式发起新代次；`complete(generation_id,source,now)`只接受当前代次及匹配的unit/job/dependencies；`revise(expected_revision,dependencies,now)`只在依赖改变时追加revision并转stale、清除当前结果；`fail`不自动重投。
- [x] 覆盖错代次/错来源、A→B→A、失败显式再发起、严格revision/time/输入与不可变对象；相同依赖为CAS校验后的no-op。
- [x] Run `uv run --locked pytest tests/unit/backend/test_workbench_render_units.py -q`，Expected全部通过；源码/分支覆盖>90%。

## Task 2：整书计划的局部失效

**Files:** Create `backend/src/speechnexus_backend/workbench/domain/book_render_plan.py`; Test `tests/unit/backend/test_workbench_book_render_plan.py`。

**Interfaces:** ConsumesTask1的Unit/Dependencies/Pin。Produces `BookRenderPlan(book_id,revision,casting_revision,units:tuple[RenderUnit,...])`、`revise_unit(unit_id,dependencies,expected_revision,now)`、`recast(role_id,voice,expected_revision,now)`和`stale_ids`。该非空计划是已有单元的渲染快照，不冒充可为空的Book草稿或导出清单。

- [x] 先写RED：三单元/两角色中改文仅一单元stale，换声仅匹配角色，未改单元保持同一对象/源结果；casting_revision仅实际换声递增。
- [x] 实现书/单元/章节ordinal/角色声音一致性、tuple不可变快照、严格CAS、整体单调时间；不隐式排队或调用语音服务。
- [x] 覆盖重复ID/章节ordinal、跨书混入、同角色冲突声音、未知目标、空计划、bool/CAS、无效时间与no-op；A→B→A不复活旧输出。
- [x] Run `uv run --locked pytest tests/unit/backend/test_workbench_render_units.py tests/unit/backend/test_workbench_book_render_plan.py -q --cov=speechnexus_backend.workbench.domain.render_units --cov=speechnexus_backend.workbench.domain.book_render_plan --cov-branch --cov-fail-under=90`，Expected全部通过且>90%。

## Task 3：终审、回归与交接

- [x] Python format/lint/mypy/imports与原样unit/contract通过；C旧192文件除目标/索引外保持hash。无数据库/机器契约/Web变更，复用上一集成262探针、28契约/drift、530Web/四条Chromium证据并保持原适用范围。
- [x] 源冻结后一次fresh sol_worker high只读终审，按上述Review Focus；Critical/Important一轮TDD修复，Minor记账，不再次分派复审。
- [x] 更新handoff/goal/索引、#42子任务和状态；累计198文件包干净基线apply/字节/本地链接/index不变。完整goal和C07仍active/open。
