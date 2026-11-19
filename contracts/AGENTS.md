# 契约目录规则

继承[根规则](../AGENTS.md)。新增或改变协议、状态、错误和生成消费者时，使用 [speechnexus-contract-change](../.agents/skills/speechnexus-contract-change/SKILL.md)。契约全貌见 [README.md](README.md)，只读受影响的文件与引用闭包。

- 先冻结生产者和消费者的机器契约，再新增业务路由；定义默认值、单位、范围、状态与稳定错误码。
- 公共协议与内部执行/部署协议分开建模；公共 DTO 不包含内部端点、实例身份、凭据、vendor handle 或内部路径。
- Python / TypeScript 边界类型由契约生成或校验；公共 DTO、内部 DTO、领域对象与 vendor DTO 在边界转换，生成类型不进入 domain。
- OpenAI 兼容范围限于声明的子集；扩展使用 `nexus` 命名空间，不改变标准字段含义。请求未知字段按契约拒绝。
- 每次状态或错误变更补正例、负例及必要事件序列；检查默认值、重放、失败和终态语义。schema 语法通过不能单独证明语义冻结或路由可用。
- 删除拒绝错误目录是穷举声明，不是示例：`shared/deletion.schema.json` 的 `x-speechnexus-errors`（`code` → HTTP 状态）与 `x-speechnexus-retryable-errors`（回答 `error.retryable=true` 的 `code` 子集）覆盖 Speech 三个删除操作的非 2xx 拒绝响应；`workbench-openapi.yaml` 同名扩展按操作覆盖 Workbench 删除专属拒绝码，跨操作信封码（`resource_not_found`、`idempotency_conflict`、`store_unavailable` 等）不在该目录，仍由该操作的通用错误响应声明。成功响应内 `Deletion.error` 携带的资源内错误（如 `deletion_stop_unconfirmed`）另由 `x-speechnexus-resource-errors` 声明，不进入拒绝目录；标为 `reserved` 的条目表示发射者属于尚未交付的切片。新增或删除拒绝码须同时改发射调用点与目录；没有发射调用点的条目必须删除。`scripts/dev-env/check-route-conformance.py` 按操作双向比对目录与真实调用点（`raise`/`deletion_error`/`_error` 调用，不是类或表声明），检查代码多出、目录多出、状态不符、可重试性不符，并给出代码 `文件:行`；常量或 f-string 间接拼出的 code 在该静态扫描之外。
- 跨文件 `$ref` 相对于包含它的契约解析；共享域复用 canonical 定义，不复制语义副本。不兼容变化记录 ADR 并同步受影响消费者。

验证使用[验证指南](../docs/agent-verification.md)的机器契约行；涉及兼容子集、生成产物或协调事务时增加对应检查。
