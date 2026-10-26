# 契约目录规则

继承[根规则](../AGENTS.md)。新增或改变协议、状态、错误和生成消费者时，使用 [speechnexus-contract-change](../.agents/skills/speechnexus-contract-change/SKILL.md)。契约全貌见 [README.md](README.md)，只读受影响的文件与引用闭包。

- 先冻结生产者和消费者的机器契约，再新增业务路由；定义默认值、单位、范围、状态与稳定错误码。
- 公共协议与内部执行/部署协议分开建模；公共 DTO 不包含内部端点、实例身份、凭据、vendor handle 或内部路径。
- Python / TypeScript 边界类型由契约生成或校验；公共 DTO、内部 DTO、领域对象与 vendor DTO 在边界转换，生成类型不进入 domain。
- OpenAI 兼容范围限于声明的子集；扩展使用 `nexus` 命名空间，不改变标准字段含义。请求未知字段按契约拒绝。
- 每次状态或错误变更补正例、负例及必要事件序列；检查默认值、重放、失败和终态语义。schema 语法通过不能单独证明语义冻结或路由可用。
- 跨文件 `$ref` 相对于包含它的契约解析；共享域复用 canonical 定义，不复制语义副本。不兼容变化记录 ADR 并同步受影响消费者。

验证使用[验证指南](../docs/agent-verification.md)的机器契约行；涉及兼容子集、生成产物或协调事务时增加对应检查。
