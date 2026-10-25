# C07 固定依赖与局部失效交接

核验日期：2026-10-24（Asia/Shanghai）。`base_sha=head_sha=2874c839f81a52cd079654372785a627dbea59a0`，分支 `codex/team-c-c00-versions`。本单元实现内部领域核心；未创建commit/push，未改公共契约、生成产物、数据库、Web或运行配置。完整C07与C00–C08目标继续进行。

## 行为与边界

- `RenderDependencies` 固定原稿/工作稿、章节/角色、声音/词典、模型及绑定、profile与文本规划版本。正文逐码点保留，内部SHA-256包含所有依赖，规范化等价字符与换行差异不会被合并。拒绝NUL、surrogate、超过200000码点或空白文本，错误与repr不泄漏正文。
- `RenderUnit` 通过严格revision CAS显式生成或修改；代次匹配后才能接纳固定的Job/Artifact/receipt引用，错unit/Job/指纹、旧代次、终态重写均拒绝。失败后须显式新代次，不能自动重投。完整生成身份历史保留，禁止复用旧Job或代次，A→B→A不会复活旧输出。
- `BookRenderPlan` 是非空已分段单元快照。修改正文只失效目标；换角色声音只失效该角色的单元。其他单元保留相同对象及结果，casting计数不会进入单元指纹。原顺序保留，重复unit/章节ordinal、跨书混入、同角色冲突声音明确拒绝。
- 无变化请求仍校验CAS和时间；相同依赖/声音返回原对象，不递增版本。时间必须aware且不早于整体最新单元，保存为UTC以消除可变tzinfo对快照的影响。bool/float不能冒充revision，容器及嵌套pin必须不可变且类型正确。
- `ready` 仅表示接受当前代次的源引用，不是已校验副本、质量确认或可导出资格。没有对象读写、语音任务派发、自动取消、GPU或引擎调用；没有修改独立副本、底座任务、审计或计量事实。

两个模块位于 `backend/src/speechnexus_backend/workbench/domain/`：`render_units.py` 和 `book_render_plan.py`；两个针对性测试位于 `tests/unit/backend/`，分别以 `test_workbench_render_units.py` / `test_workbench_book_render_plan.py` 命名。仅依赖标准库及既有领域revision校验，依赖方向保持向内。

## 验证

使用现有Python3.14.9，先分别观察缺模块RED，再实现并运行GREEN。针对性命令：

```bash
uv run --locked pytest tests/unit/backend/test_workbench_render_units.py tests/unit/backend/test_workbench_book_render_plan.py -q --cov=speechnexus_backend.workbench.domain.render_units --cov=speechnexus_backend.workbench.domain.book_render_plan --cov-branch --cov-fail-under=90
uv run --locked pytest tests/unit tests/contract -q
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy
uv run --locked lint-imports
git diff --check
```

针对性 **90 passed**，两个新模块语句/分支覆盖 **100%**。格式检查432文件、mypy320源文件、lint及7项依赖边界通过。使用临时源码副本反转当前代次比较，测试29失败/61通过；工作区源文件hash不变，证明测试能发现该屏障被破坏。最终原样全库unit/contract **1749 passed**（27.52s，无skip）；一次fresh独立终审无Critical/Important/Minor；审查者另跑90项并实测错代次/来源、Unicode及可变tzinfo边界，四个源码/测试hash保持一致，无需修复或再次复审。

本轮未改变数据库、机器契约、生成器或Web；这些路径均与[2874集成核验](2026-10-24-team-c-2874-integration-handoff.md)逐字节一致，复用其PG262、28份契约/drift、Web530和四条正式Chromium合成流程证据，适用范围不扩大。新C07模块没有实际持久化/浏览器/模型验证，不能用旧探针替代。图谱覆盖复核返回project not found，直接读取具名源码，没有建立索引。

日志、RED/GREEN、mutation、终审和源hash位于本机忽略目录 `.runtime/team-c/c07-render-*`。原192文件包保留，累计包新增本计划/交接及四个代码测试文件，仅更新目标/索引两份既有文档；累计198文件包经干净基线apply、全部字节、280条本地链接/锚点及真实index不变核验；旧190文件保持原hash，仅目标/索引两份文档更新。逐文件manifest和补丁SHA-256位于 `.runtime/team-c/team-c-current.manifest.json`，对应补丁为 `.runtime/team-c/team-c-current.patch`；不是远端已发布制品。

## 后续与责任

1. C继续TXT导入、章节/角色持久映射、领域状态原子CAS/生成身份唯一约束和删除恢复。当前快照不代表Book草稿、完整有声书存储或导出清单；领域pin尚须在application层核对权威源、binding及声音资格。
2. 消费同版公开语音生成边界，固定真实Job/Artifact/receipt。A00生成runtime入口/公共接线、迁移框架/组合根由A协调，C不手抄wire字段或修改底座表。
3. 明确选择保存后，经公开下载API校验源Artifact摘要/receipt、写入工作台自己的saved_assets并原子提交。源过期/复制未完成不能取得可导出资格，原成功SpeechJob不改成失败。
4. C实现独立质量/试听/匿名确认、固定casting与manifest的有界export worker、检查点/取消/恢复及staging原子发布，再完成页面和真实资格。export不调用推理引擎，不借用realtime预算；有声书不等待C06助手。

生成身份历史的存储/检查随显式重做次数增长，后续持久化需保留唯一约束；本轮没有服务准入/容量或全书规模资格。没有以局部测试勾选完整C07父任务、保存/导出或G4。#42状态和内部子任务按本单元实际证据更新，所有C工作包继续open，Goal继续active。

[实施计划](2026-10-24-team-c-c07-render-plan.md) · [团队目标](2026-10-24-team-c-goal.md) · [当前集成](2026-10-24-team-c-2874-integration-handoff.md)
