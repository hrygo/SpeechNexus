---
name: speechnexus-contract-change
description: "变更或审查 SpeechNexus 机器契约、协议状态/错误及生成消费者；新增业务路由前也使用。"
---

# 契约变更与消费者交接

交付语义一致的权威契约、样例和受影响消费者。审查请求保持只读，实施请求完成范围内修改；技能不授权合并、发布或运行态变更。

## 固定协议影响

从[契约索引](../../../contracts/README.md)定位目标机器文件，遵守[契约目录规则](../../../contracts/AGENTS.md)。先确定生产者、消费者、公开/内部边界，以及现有版本和兼容基线；不要因为只修改一个 schema 就忽略其引用者。

按任务补读：

- 生成类型或校验器：[生成契约包](../../../packages/contracts/README.md)。
- 端口、表或应用归属：[所有权矩阵](../../../docs/architecture/contract-ownership.md)。
- OpenAI 标准字段、事件或 SDK 行为：[兼容子集](../../../docs/architecture/openai-compatibility.md)。
- 版本化 HTTP 源：[HTTP 结构变化检查](../../../docs/architecture/http-contract-change-checks.md)，选择对应表面与固定基线；升版本不豁免发现，结构检查不替代语义验收。
- 协调事务或未知执行语义：同时使用 [speechnexus-coordination-change](../speechnexus-coordination-change/SKILL.md)，不把事务不变量压缩成 schema 形状检查。

现有有效契约已经明确的事项直接沿用。只有缺少会改变公共语义、产品范围或持久状态的关键决定时才询问；先完成可以独立核实的内容。

## 更新规范与样例

实施时执行下列更新；审查时只核对差异、消费者和已有证据，不生成或改写文件。

1. 在新增业务路由前固定字段的默认值、单位、范围、状态、稳定错误和恢复动作；明确可重试、同身份重放、结果未知与终态/清理状态的区别。
2. 修改 canonical 源，复用共享域；公共协议、内部协议、领域对象与 vendor DTO 分别转换。检查公开投影不会暴露内部身份、私有端点、凭据或内容。
3. 为实际改变的状态与错误补正例、负例及必要事件序列，覆盖未知字段、边界值、终态一致性与相关恢复路径。涉及不兼容变化时记录 ADR、版本和消费者升级边界。
4. 生成消费者受影响时，先检查生成目标已有 diff，再运行 `make contracts-generate` 并审阅输出；不手改生成产物或增加独立语义副本。同步生产者、实际消费者与有效文档，保留任务外改动。

## 验证与交付

实施时按[验证指南](../../../docs/agent-verification.md)运行机器契约及受影响语言检查；生成路径运行 `make contracts-drift`。涉及 OpenAI 兼容子集时增加 SDK 与 `CONTRACT_BASE_REF` 兼容检查，基线使用核实过的目标分支。只读审查沿用现有有效证据，未要求执行测试时列出验证缺口。

交付说明语义变化、生产者/消费者覆盖、兼容与升级影响、实际检查和仍未接入的消费者。schema、SDK mock 或生成包通过，只证明对应协议覆盖；没有运行路由、真实引擎或目标拓扑证据时保留未验证状态。
