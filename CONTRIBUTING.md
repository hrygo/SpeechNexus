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

先搜索已有 issue 和开放 PR，确认尚未交付的具体问题。标题描述结果或行为，一个实施 issue 对应一个可独立验收的交付；尚未准备实施的要求保留在 PRD 和需求矩阵。缺陷、建议和实施任务分别使用对应模板，不把外部报告改造成内部流程表。

[实施模板](.github/ISSUE_TEMPLATE/03-implementation.md)采用以下栏目；这是项目选择，并非 GitHub 强制的统一格式。

| 部分 | 内容 |
| --- | --- |
| 背景与目标 | 当前问题、影响、已有事实与预期结果；链接相关 PRD 需求和设计 |
| 范围 | 本次交付的边界；仅在容易误解时说明排除项 |
| 验收标准 | 可观察的输入、行为和结果，以及本问题必要的失败或边界条件 |
| 依赖与实施提示（可选） | 真正需要的接口、版本、制品、提供方和解除条件；必要时给简短实施步骤 |

验收条件描述用户或系统结果，不用“完成实现”“测试全绿”替代，也不把同一要求复制成任务清单与 AC。实施步骤确有价值时再列；步骤完成不等于验收通过。小任务可以只有几句话，不要求进展摘要、固定字符预算或无关阶段。

负责人、标签、里程碑及原生父项使用 GitHub 字段。正文就地维护当前事实和必要证据，保留原报告、有效阈值、失败条件与唯一依据；详细日志链接到已有 PR 或报告。查询和建议保持只读；已授权写入时先重读最新版，只改授权字段并回读。无事实变化不重复写入，结果未知先查原卡。

默认一个小 issue 由一个 PR 完整解决。项目以[总控 #336](https://github.com/hrygo/SpeechNexus/issues/336)和十个 MECE Epic（#337–346）组织范围；维护者创建的新项目 issue 必须原生直属唯一 Epic，跨域协作记录依赖，不重复建卡或增设中间层。需求先记录在 [PRD](docs/prd.md)、相关设计和[需求矩阵](docs/plans/delivery-tracking.md)。需要选择下一切片并按需建卡时使用[按需规划技能](.agents/skills/speechnexus-next-delivery/SKILL.md)；范围已确定的普通建卡直接按本指南执行，仍须核验原生父 Epic 并登记需求关联。未建卡不表示遗漏；Epic 不罗列已取消的历史任务。外部反馈保留原报告，由维护者按授权分类关联。

## 状态、标签与关闭

GitHub 的 `open` / `closed` 和关闭原因表示 issue 生命周期。负责人、里程碑和 Projects 字段按实际需要维护；若采用 Projects 管理工作状态，以其字段为准，避免再建一套同义状态。

标签用于分类与筛选，按仓库已有词汇维护。`bug`、`enhancement`、`documentation` 等只是常用例子；`good first issue` 应有清晰范围与入手指引，`help wanted` 应有明确可参与的工作。模板不预设未经核实的标签、负责人、组织 issue type 或 Projects。

已经使用 `status:*` 的项沿用现有体系，活动项保留一个状态标签；普通反馈不强加标签流程。状态按实际剩余工作选择，无需逐级流转，不另建同义 Projects 字段或状态标签。

| 现有标签 | 含义 |
| --- | --- |
| `status:planned` | 尚未开始，必要输入仍待准备 |
| `status:ready` | 开工输入齐备；不代表运行能力 ready |
| `status:in-progress` | 仍有实现工作 |
| `status:integration` | 实现已交付，仍待正式接线或联调 |
| `status:qualification` | 相应集成已完成，仍待适用验收 |
| `status:blocked` | 关键下一步被明确前置阻断，写明负责人、影响和解除条件 |
| `status:done` | 完整范围和 AC 已满足，对应 `closed` / `completed` |

授权覆盖状态维护时，取消或不计划关闭应去掉误导性的活动/`done` 标签，保留其他分类；不创建新的状态体系。证据按实际版本、环境和覆盖判断，CI、fake、单库或逻辑集群不替代原要求；也不为小修正增加原 AC 未要求的额外资格门槛。

| 处理结果 | 关闭方式 |
| --- | --- |
| 问题已解决，或完整实施范围与 AC 满足 | `completed`；文档、答疑和无需代码的解决也可附结论后关闭 |
| 明确暂不计划、取消或不在范围内 | `not_planned`，说明原因；不标记为已交付 |
| 重复问题 | 链接唯一主 issue；接口支持时使用 `duplicate`，否则按可用关闭原因并明确说明重复 |
| 尚缺实现或必要验收 | 保持 `open`，写明下一步或具体阻塞 |

关闭和重开应保留可追溯的依据。信息不足先询问具体问题，不因模板不全、没有回复或没有进度自动关闭。合并同步不重开已明确取消、拒绝或重复关闭的 issue。

PR 正文只有在该 PR 将完整解决 issue 时才写 `Closes #N` / `Fixes #N`；部分交付写 `Refs #N` 并注明剩余范围。**PR 描述中的关闭关键字仅对目标为仓库默认分支的 PR 生效**；其他集成分支须按实际合并与验收结果手动同步。合入默认分支会自动关闭关联 issue，因此仍需额外验收的项应使用普通引用。PR 评论中的关闭关键字不替代正文关联。

## 合并后收尾

1. 核实远端 PR 已合入目标主干及实际集成版本，并检查相关回退；只关闭 PR 或合入其他分支不代表交付。
2. 核对直接关联 issue 的完整范围与 AC。有剩余工作则维护当前差距；完整满足后按授权关闭。已正确自动关闭且正文、状态一致时结束，不重复开关或追加评论；原因不明的关闭不自动重开。
3. 仅更新受影响需求在[关联源](docs/plans/delivery-issues.json)中的实现、集成、验证和证据，再运行 `python3 scripts/dev-env/refresh-implementation-tracker.py` 只读刷新矩阵。普通独立反馈不要求刷新全表；子项关闭不自动关闭 Epic，提供者合并不代表消费者已接入。
4. 本次承担交付收尾时，按[工作区收尾](docs/operations/agent-delivery-sop.md#工作区生命周期与收尾)清理已获授权的临时资源；仅同步其他人的 issue 时不顺带清理其工作区。

## 提交 PR 与验证

先读[开发指南](docs/development-environment.md)及 [AGENTS.md](AGENTS.md)，按[验证指南](docs/agent-verification.md)执行与改动匹配的检查。PR 描述说明解决的问题、关联 issue、实际验证及覆盖限制；有剩余工作或风险时明确写出。小 PR 无需为了模板增加无关内容。

测试使用合成或最小脱敏数据。纯文档、无脚本 skill 和 issue 模板的本地验证只需检查链接、格式、规则衔接与配置结构；无需启动数据库、模型或完整业务验证。CI 范围遵循[现有门禁](docs/validation/ci-validation.md)。未运行的检查如实注明。

## 规则维护与官方依据

Issue 正文、状态与合并收尾规则以本页和[仓库模板](.github/ISSUE_TEMPLATE/)为唯一维护入口；选择下一交付时使用[按需规划技能](.agents/skills/speechnexus-next-delivery/SKILL.md)。修改时同步模板及有效引用，历史计划保留原时点事实。

以下 GitHub 官方文档核验于 **2026-11-03**；模板栏目、按需建卡、十个 Epic 及 `status:*` 名称属于本项目约定。

- [Issue 表单语法](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-issue-forms)与[表单字段 schema](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-githubs-form-schema)：输入、必填项和可选元数据。
- [配置模板选择器](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/configuring-issue-templates-for-your-repository)：模板在默认分支生效，允许保留空白入口。
- [Projects 最佳实践](https://docs.github.com/en/issues/planning-and-tracking-with-projects/learning-about-projects/best-practices-for-projects)：按需要拆分、建立关系与保持信息来源一致。
- [Issue 模板与表单](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/about-issue-and-pull-request-templates)：按贡献者需要选择模板或结构化表单，合入默认分支后生效。
- [原生 sub-issues](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-sub-issues)：通过父项创建、关联或用支持 `--parent` 的 GitHub CLI 创建，正文链接不替代关系。
- [关联 PR 与 issue](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue)：关闭关键字与默认分支语义。
- [关闭 issue](https://docs.github.com/en/issues/tracking-your-work-with-issues/administering-issues/closing-an-issue)与[更新 issue API](https://docs.github.com/en/rest/issues/issues#update-an-issue)：关闭原因、重复目标和字段更新语义；具体工具能力需实测。
- [私密报告安全漏洞](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/report-privately)：报告入口取决于仓库是否启用；缺少入口时按政策或请求私密联系方式。
