# CI 路径路由实施计划

> **For agentic workers:** 按任务顺序实施，先验证失败用例，再实现并复核整个分支。

**Goal:** 缩短文档与 Web PR 的反馈时间，保留完整业务改动的验收门禁。
**Architecture:** 增加一个仅依赖 Python 标准库的路由作业；按合并结果相对目标提交的真实 Git 差异选择 docs、web、full。现有必需检查验证路由及其对应结果，不把意外跳过视为通过。
**Tech Stack:** GitHub Actions、Git、Python 3.12+、unittest / pytest。
**Spec:** 本文件的“设计与边界”。

## 设计与边界

- 仅 PR 可缩窄范围。`main` push、手动执行、空差异、未知事件或无法确认差异均选 full。
- docs 仅允许根 README / AGENTS 和 `docs/` 内 Markdown；web 允许已知 Web 源码/静态资产及 docs。其他路径，尤其 CI、脚本、测试、依赖、契约、迁移和部署，选 full。
- docs 运行路由回归及差异检查；web 仍运行现有 Python 静态/单元与 Web 完整检查，省去双数据库分片；full 保留现有全部检查。
- 保留三个受保护检查名及所有覆盖率、双数据库分片和制品完整性要求。分类失败、非法输出、该跑却跳过、失败或取消均不能放行。
- 使用事件的准确 base SHA 和当前 checkout；NUL 分隔路径、不展开 shell 输入、重命名同时检查旧/新路径。不引入第三方路由 action、服务或依赖缓存。
- 按 GitHub 官方建议使用作业条件，不设置导致必需检查长期 Pending 的 workflow 级 paths 过滤。

## Review Focus

- 删除/重命名 Python 文件到文档目录，仍须 full。
- docs + 未知或敏感路径混合时，仍须 full。
- 缺失/非法 base 或路由输出，不能形成轻量通过。
- docs/web 合法省略与 full 意外 skipped 必须区分；failure/cancelled 不能通过。
- `main` / 手动触发和 CI 自身变更不能因白名单绕过全量。

## Task 1: 标准库路由与边界回归

**Files:** 新建 `scripts/dev-env/ci-routing.py`、`tests/ci/test_ci_routing.py`。
**Interfaces:** `classify(paths) -> str`；`detect_route(event, base, cwd) -> tuple[str, str]`；CLI 写入封闭枚举 `mode=docs|web|full`。

- [x] 写真实临时 Git 仓库、重命名/删除、混合路径、事件与缺失 base 的回归并观察失败。
- [x] 实现分类与精确 Git 差异；运行 `python3 -m unittest discover -s tests/ci -v`，预期全部通过。

## Task 2: 条件作业与受保护结果

**Files:** 修改 `.github/workflows/ci.yml`、`tests/contract/test_ci_workflow.py`、`tests/unit/test_integration_shards.py`。
**Interfaces:** 消费 Task 1 的 mode；所有最终检查同时验证路由成功与所需作业状态。

- [x] 扩展实际汇总 shell 的成功/失败/取消/跳过组合回归，先观察失败。
- [x] 接入路由，完整模式保留现有命令；文档不安装依赖或启动服务，Web 保留 Python 消费者验证。
- [x] 运行针对性 pytest、Ruff、mypy、actionlint 和 `git diff --check`，预期通过；独立审查后以 PR 验证全量路径。

## Task 3: 运行证据与维护说明

**Files:** 新建 `docs/architecture/ci-validation.md`，本计划仅标记最终结果。

- [ ] 在路由合并后用维护说明的纯文档 PR 实测轻量路径，核对三个受保护结果及实际省略作业。
- [ ] 分开报告执行时间与排队影响，不用单次运行承诺长期 SLA；复核自动删除分支和清理后的对象。

## 核验依据

核验日期：2026-10-25。基线为 [PR #138 CI](https://github.com/hrygo/SpeechNexus/actions/runs/40155498616)：13 项作业全部成功，整轮 821 秒，作业累计执行 1584 秒。

- [GitHub 作业条件与 skipped 语义](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-jobs-with-conditions)
- [GitHub workflow 路径过滤与 Pending 风险](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore)
