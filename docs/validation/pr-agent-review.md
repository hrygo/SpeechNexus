# PR-Agent 自动审查

该独立工作流默认关闭。需要启用时，仓库管理员先自行配置 Actions secret
`OPENCODE_API_KEY`，再将 repository variable `PR_AGENT_REVIEW_ENABLED` 设为精确的
`true`；删除变量或改为其他值即可停止自动运行。缺少 secret 时作业明确跳过审查。
不通过评论触发；仅同仓库、非草稿、非 bot 且作者具有 OWNER、MEMBER 或
COLLABORATOR 身份的 PR，在打开、重新打开、转为可审查或推送更新时触发。
fork PR 不运行。工作流由 PR 事件执行，不检出或运行 PR 的代码；同仓库工作流的
修改权限仍应只授予可信人员。

每次审查会向 OpenCode Go 的 `glm-5.3-flash` 发送 PR diff 和审查所需元数据，输出
发布到 PR。不要在 PR 中放入不允许发送给该服务的数据。审查可能产生费用；一次
review 可产生多个模型请求，同一 PR 的更新也会重新触发。此开关不是预算或用量
上限，维护者须自行评估费用与数据处理条款。按仓库 ID 和 PR 编号生成稳定的
OpenCode session；不使用其他模型回退。工作流不读取 PR 的 PR-Agent 配置、仓库
上下文文件或关联工单，不调用 describe/improve，也不设置标签。GitHub 临时令牌
只授予 contents read 和 pull-requests write。

运行时固定为 [PR-Agent v0.49.0](https://github.com/The-PR-Agent/pr-agent/tree/efdde4afe14aa401a3889012c7f3e9ef3081a430)
的 CLI 镜像 OCI digest，避免 Action 包装器的滚动镜像。升级时核对官方版本镜像
digest、配置键、权限和 OpenCode header，再更新工作流与离线安全测试。离线测试
不能证明 secret、服务可用性或实际审查成功；本变更未执行付费请求。
