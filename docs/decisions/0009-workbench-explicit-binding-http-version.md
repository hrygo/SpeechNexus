---
title: 工作台显式模型绑定与配置发现的 HTTP 版本迁移
status: accepted
date: 2026-10-25
---

# ADR 0009 工作台显式模型绑定与配置发现的 HTTP 版本迁移

## 背景

工作台 v1 的 `WorkbenchStatus`、`RenderUnit` 均为
`additionalProperties: false`。直接新增 `minutes_profile`、`model_binding`
会使合法的新响应被旧消费者拒绝；将 `model_binding` 加入原创建请求的必填项，
也会拒绝原来合法的请求。生成类型、抬高文档版本或修改比较器不能消除这些变化。
相对于 `5f54f30` 的六个 HTTP 兼容发现均属于真实协议变化。

持久渲染意图必须包含调用方明确选择的绑定 ID 和 revision；恢复不得读取可变默认值
补出绑定。纪要消费者也需要服务端批准的模板 revision 与模型名，不能自行猜测。
基线仅声明工作台 v1 协议，没有 `backend/workbench` 生产实现，因此没有可继续维护的
旧版创建执行器，也没有需要迁移的 v1 生产创建回执。

## 决定

保留 v1 的原 schema、operation 名、已声明成功响应及严格未知字段校验，
将需要新增字段的操作以新增 `/v2/workbench` 路径交付。源文档版本维持原值，
版本选择由路径明确表达；新版本不豁免旧路径的兼容检查。

| 新操作 | 路径 | 新协议要求 |
| --- | --- | --- |
| `getWorkbenchStatusV2` | `GET /v2/workbench/status` | 使用 `WorkbenchStatusV2`；可发现受约束的 `minutes_profile` |
| `listRenderUnitsV2`、`createRenderUnitV2` | `/v2/workbench/chapters/{chapter_id}/render-units` | 列表项目、创建结果均为 `RenderUnitV2`；创建显式必填 `model_binding` |
| `getRenderUnitV2`、`updateRenderUnitV2` | `/v2/workbench/render-units/{unit_id}` | 响应必须包含固定绑定；PATCH 使用 `RenderUnitPatchV2` |
| `generateRenderUnitV2` | `POST /v2/workbench/render-units/{unit_id}/generations` | 接收回执的嵌套单元使用 `RenderUnitV2`；仍为契约声明，尚无生产路由 |

`minutes_profile` 的缺省或 null 表示不能从状态发现可用纪要配置；消费者禁用新建，
不能猜测模板或模型。`llm_enabled=false` 时不得返回已批准配置。
这不改变已有纪要意图的原身份重放与恢复规则。

v1 GET、PATCH 和新列表投影只返回 v1 允许的字段，同一持久单元的绑定继续保存在
内部及 v2 视图中。v1 创建先用原请求 schema 校验，再返回已有稳定错误
`503 workbench_unavailable`，不调用创建服务、不写入半成品、不构造默认绑定。
源契约补充实际边界使用的 400/408/413/415/500/503 响应；不移除原有成功或错误响应。
客户端可显式迁移到 v2，不得在失败后自动改版本、改 key 或修改原意图重试。

`pronunciation`、`profile` 的写入仍未实现。v1/v2 创建和 PATCH 只要显式包含
任一字段（包括 PATCH 的 null 清除），即返回现有 `422 invalid_change`，不执行
请求中的其他修改、不调用存储。保留完整机器契约，不静默丢弃这些选项，
也不据此将原 Goal 的发音表或配方能力标为完成。

## 消费者与升级

`contracts/bindings.json` 同时选择 v1/v2 schema 与 HTTP 操作；Python 与 TypeScript
类型及运行时验证从同一源生成。`WorkbenchClient.status()`、渲染单元列表、创建和
修改使用 v2 路径与 V2 类型；书籍、章节、纪要及其他未改协议的操作继续使用 v1。
Admin 的纪要配置发现也使用 v2 状态。原 v1 schema 不被 V2 类型别名替换。

共享 HTTP 正负例分别验证旧无绑定创建请求仍被接受、旧响应拒绝新增字段、v2
请求拒绝缺失/错误绑定，以及生成回执的嵌套版本。ASGI 回归验证 v1 创建的合法请求
确实得到稳定 503 且零写入，v2 必填绑定透传至应用，状态/渲染响应按路径投影。
生成/契约测试不证明生成操作已注册、真实语音引擎可用或多节点部署已取得资格。

## 取舍与回退

需要维护少量 v1 投影和新增路径，换取可检查的迁移边界。放宽未知字段、可选绑定、
从声音/模型推断绑定及仅提升 `info.version` 均不能满足显式意图与旧消费者要求。
不实现没有业务用途的无绑定“可生成”单元，也不为基线中不存在的生产执行器
补造历史成功语义。

本变更不修改持久 schema、执行命令、绑定值或既有幂等账本。回退 UI 时应一并保留
支持 v2 的服务端；v1 仍可读取和修改其允许的投影，但不能创建渲染单元。
停用新增路由前须先停止 v2 新建并核对消费者；保留持久意图与回执，不能通过删除
已有数据恢复旧协议。
