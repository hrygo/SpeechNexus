# Web 目录规则

继承[根规则](../AGENTS.md)。设计基线见 [UI/UX 与 Token 框架](../docs/architecture/ui-ux-design-framework-and-tokens.md)，workspace 与模块说明见[工程目录](../docs/architecture/repository-layout.md#3-web-工作区)。

- `admin` 与 `workbench` 是独立入口；它们不形成身份或数据隔离。界面不得暗示登录、个人私有空间或未实现的权限边界。
- `shared/` 只放两个入口以相同语义使用的稳定组件与设计 Token；工作台业务编排留在 `workbench`。
- 子应用复用共享 Token，遵守键盘焦点、减少动效与本地资源规则；资源、代理、错误报告和观测同样遵守内容不出部署网络的要求。
- 能力状态使用 `ready` / `contract` / `planned`。未实现能力使用明确状态或空态；不以虚构任务、用户、模型或容量填充产品界面。
- 消费权威机器契约生成或校验的边界类型，不在前端手写独立协议；客户端不能提交任意 LLM 端点。协议变化使用 [speechnexus-contract-change](../.agents/skills/speechnexus-contract-change/SKILL.md)。
- 共享依赖由根 `pnpm-workspace.yaml` catalog 管理；依赖变更同步 lockfile。

验证使用[验证指南](../docs/agent-verification.md)的 Web 行；只有跨到契约或持久化边界时才增加相应检查。
