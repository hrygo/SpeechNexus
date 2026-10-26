# 贡献与 Issue 指南

欢迎报告问题、提出建议、改进文档或提交代码。Issue 和 PR 可以使用中文或英文。当前产品能力与限制以 [README](README.md) 为准。

## 提交反馈

先搜索已有的 open / closed issue；相同问题优先补充新版本、复现或证据。一个 issue 聚焦一个问题，标题描述具体行为与影响，例如“取消转写后任务仍显示运行中”。无需添加优先级或状态前缀。

| 入口 | 需要提供的信息 |
| --- | --- |
| [缺陷报告](.github/ISSUE_TEMPLATE/01-bug-report.yml) | 版本或 commit、相关环境、复现步骤、预期与实际结果；无法稳定复现时提供触发条件和频率 |
| [功能建议](.github/ISSUE_TEMPLATE/02-feature-request.yml) | 使用场景、遇到的困难与希望获得的结果；实现方案和替代做法可选 |
| [实施任务](.github/ISSUE_TEMPLATE/03-implementation.md) | 已明确的问题、范围、任务与可验证的验收标准；面向准备实施的贡献者和维护者 |
| 空白 issue | 文档问题、使用疑问或不适合上述表单的反馈；描述问题与已尝试的方法即可 |

外部反馈无需先设计解决方案、拆实施步骤、填写进度或理解内部 Gate。表单提供的信息不足时，维护者会指出具体缺口；提交反馈不代表已经承诺排期。简单修正可以直接提交 PR，影响产品范围、公共协议或持久数据的改动宜先讨论。

公开内容只使用合成或最小脱敏示例。日志、截图和附件应去除凭据、私有地址、私人音频、完整业务转写、声音特征及内部模型路径。无需提供邮箱或完整环境变量。

安全漏洞请先查看仓库 **Security / Security and quality** 页：如有 **Report a vulnerability**，使用该私密入口；没有可用入口时，按安全政策联系维护者，或仅公开请求私密联系方式，收到渠道后再发送细节。不要在公开 issue、PR 或附件中披露可利用细节。

## 从反馈到交付

维护者先判断问题是否属于项目范围、是否重复、是否需要更多信息，再决定是否进入实施。整理他人报告时保留原始复现、预期/实际结果和讨论依据；需要较大实施计划时链接独立任务。

实施任务使用以下四部分。这是 SpeechNexus 的内部交付约定；缺陷报告、功能建议和使用疑问沿用各自的格式。

| 部分 | 内容 |
| --- | --- |
| 目标 | 问题、期望结果与必要范围边界 |
| 进展总结 | 当前事实、证据链接、剩余工作和实际阻塞；尚未开始时一句话即可 |
| 任务清单 | 为交付结果需要完成的步骤 |
| 验收标准（AC） | 可观察、可验证的结果，以及本问题所需的失败/边界条件 |

任务完成与验收通过分别勾选。小任务各部分可以只有一句话或一项，不增加与本问题无关的阶段。正文是当前摘要，讨论、决定和详细证据保留在相关评论、PR 或报告中；重要决定须可追溯，整理时保留仍有效的唯一证据。

默认一个小 issue 由一个 PR 完整解决；确有独立交付边界或多个贡献者协作时，才拆 sub-issues。GitHub 原生父子关系和 blocked by / blocking 关系可用时优先使用；普通相关链接不自动代表阻塞。Epic、工作包、Gate 与编排矩阵只服务于实际存在的大型交付。

## 状态、标签与关闭

GitHub 的 `open` / `closed` 和关闭原因表示 issue 生命周期。负责人、里程碑和 Projects 字段按实际需要维护；若采用 Projects 管理工作状态，以其字段为准，避免再建一套同义状态。

标签用于分类与筛选，按仓库已有词汇维护。`bug`、`enhancement`、`documentation` 等只是常用例子；`good first issue` 应有清晰范围与入手指引，`help wanted` 应有明确可参与的工作。模板不预设未经核实的标签、负责人、组织 issue type 或 Projects。

现有实施编排和已使用 `status:*` 的项继续沿用该体系：活动实施项维持一个现有状态标签，完成项使用现有的 `status:done`。普通反馈无需补齐这些标签。新增或迁移标签、项目字段及自动化由维护者决定；具体映射见[状态与证据](.agents/skills/sync-issues-after-merge/references/status-and-evidence.md)。

| 处理结果 | 关闭方式 |
| --- | --- |
| 问题已解决，或完整实施范围与 AC 满足 | `completed`；文档、答疑和无需代码的解决也可附结论后关闭 |
| 明确暂不计划、取消或不在范围内 | `not_planned`，说明原因；不标记为已交付 |
| 重复问题 | 链接唯一主 issue；接口支持时使用 `duplicate`，否则按可用关闭原因并明确说明重复 |
| 尚缺实现或必要验收 | 保持 `open`，写明下一步或具体阻塞 |

关闭和重开应保留可追溯的依据。信息不足先询问具体问题，不因模板不全、没有回复或没有进度自动关闭。合并同步不重开已明确取消、拒绝或重复关闭的 issue。

PR 正文只有在该 PR 将完整解决 issue 时才写 `Closes #N` / `Fixes #N`；部分交付写 `Refs #N` 并注明剩余范围。**PR 描述中的关闭关键字仅对目标为仓库默认分支的 PR 生效**；其他集成分支须按实际合并与验收结果手动同步。合入默认分支会自动关闭关联 issue，因此仍需额外验收的项应使用普通引用。PR 评论中的关闭关键字不替代正文关联。

## 提交 PR 与验证

先读[开发指南](docs/development-environment.md)及 [AGENTS.md](AGENTS.md)，按[验证指南](docs/agent-verification.md)执行与改动匹配的检查。PR 描述说明解决的问题、关联 issue、实际验证及覆盖限制；有剩余工作或风险时明确写出。小 PR 无需为了模板增加无关内容。

测试使用合成或最小脱敏数据。纯文档、无脚本 skill 和 issue 模板的本地验证只需检查链接、格式、规则衔接与配置结构；无需启动数据库、模型或完整业务验证。CI 范围遵循[现有门禁](docs/architecture/ci-validation.md)。未运行的检查如实注明。

## 规则维护与官方依据

贡献入口以本页为准；Agent 的正文维护细节集中在[正文规范](.agents/skills/speechnexus-issue-maintenance/references/issue-body.md)，合并收尾使用 [sync-issues-after-merge](.agents/skills/sync-issues-after-merge/SKILL.md)。修改时同步相关模板及入口，历史计划保留原时点事实。

以下 GitHub 官方文档核验于 **2026-10-26**；四段正文、摘要预算与 `status:*` 名称属于本项目约定。

- [Issue 表单语法](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-issue-forms)与[表单字段 schema](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-githubs-form-schema)：输入、必填项和可选元数据。
- [配置模板选择器](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/configuring-issue-templates-for-your-repository)：模板在默认分支生效，允许保留空白入口。
- [Projects 最佳实践](https://docs.github.com/en/issues/planning-and-tracking-with-projects/learning-about-projects/best-practices-for-projects)：按需要拆分、建立关系与保持信息来源一致。
- [关联 PR 与 issue](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue)：关闭关键字与默认分支语义。
- [关闭 issue](https://docs.github.com/en/issues/tracking-your-work-with-issues/administering-issues/closing-an-issue)与[更新 issue API](https://docs.github.com/en/rest/issues/issues#update-an-issue)：关闭原因、重复目标和字段更新语义；具体工具能力需实测。
- [私密报告安全漏洞](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately)：报告入口取决于仓库是否启用；缺少入口时按政策或请求私密联系方式。
