---
title: "团队 C 交接：Admin 部署诊断页（能力三态只读）"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 团队 C 交接：Admin 部署诊断页（能力三态只读）

AdminUI 此前只有静态能力卡（`ready`/`contract`/`planned` 三张写明"计划"），**没有任何一处读取部署自身的状态**：能力三态、内网模型绑定、共享部署声明在管理入口都不可见，运维只能靠猜或看日志。本单元把可读事实接到 AdminUI，同时保持"没有可读来源就不显示"的边界。

## 基线

- `base_sha` = `head_sha` = `4b08c396c0fe8fc43c7e85683e534befab6bb22b`（未提交、未推送）。
- 上一冻结单元 `C06-assistant-honest-state-browser-qualification`（463 路径，补丁 `edbe08a7…`）。
- 本单元 `C01-admin-deployment-diagnostics`。

## 本轮改动

- `web/admin/src/diagnostics/workbench-status.ts`：同源只读客户端读 `GET /v1/workbench/status`，`no-store`、`redirect:"error"`，并用**生成契约**的 `parse("WorkbenchStatus", …)` 校验；不可达/非 200/非 JSON/不符合契约分别给出稳定错误码，绝不把读取失败当成能力结论。
- `web/admin/src/diagnostics/copy.ts`：能力中文名、三态说明、**只降级的呈现映射**（available→ready、unavailable→contract、disabled→planned）、共享部署**本地化**（契约里 `shared_deployment_notice` 是固定代码 `shared_deployment`，界面必须本地化而不是回显英文）、未知状态的安全处理（`asState` 返回 null，绝不上调为可用）。
- `web/admin/src/diagnostics/DiagnosticsHost.tsx`：页面**打开时不发任何请求**，必须点击"读取部署状态"才读取（与任务中心"显式读取"一致）；读取后逐条渲染服务端返回的五个能力（名称、状态原样呈现），显示工作台启用状态与内网模型绑定（含模型与模板修订）、本地化共享部署声明，并明确列出**尚未接入**的队列深度、预算、租约、outbox、计量与 GPU 事实；读取失败时只给错误提示、不渲染任何能力卡。
- `web/admin/src/App.tsx`：挂载诊断区，并把过期的外壳/能力卡文案改为现实（诊断已接入；预算、租约与计量仍待接入），原第三张卡由 `planned` 改为 `contract`。

## 证据

| 证据 | 内容 | 结果 |
| --- | --- | --- |
| Admin 单元 8 项 | 映射只降级；未知状态按未知处理；**未点击前不发请求**；逐条渲染服务端五能力且状态原样；模型绑定不虚构；读取失败不渲染任何能力；共享部署代码被本地化（回显英文为 0）；卸载后迟到的应答不再发布 | 通过（admin 110→**118**） |
| 真实浏览器（Admin 产物）2 项 | 真实 Chromium 156.0.8078.4 + 生产 AdminUI 包 + 真实 C 组合根 + 隔离 PG：默认部署与"已配置内网模型"两种配置各起一次真实运行时，先由 HTTP 读 `/v1/workbench/status` 作为期望，再要求页面确认 | 通过 |
| 门禁 | 全量 `--browser` 探针、`pnpm format:check`、`pnpm lint`（设计 Token/文案边界/能力三态）、`pnpm typecheck`、`pnpm test`、`pnpm build` | 见本轮记录 |

浏览器断言：打开页面时 `statusReadMethods` 为空；点击后恰好一次 `GET`；五张能力卡且每张都带"服务端状态："；未配置模型时显示"未配置（默认关闭）"、配置后显示模型名；"本部署为共享部署"可见而 `shared_deployment` 英文代码不可见；`outbound=0`、`unexpectedErrors=0`、`failed=[]`。制品：`.runtime/team-c/browser/admin-diagnostics-{default,llm}/`。

## 未验证与边界

- 页面只呈现部署自报事实：**不探测引擎、不读凭据、不显示未采集的运行数据**。队列深度、预算、租约、outbox、计量、GPU 仍无来源，保持为空。
- AdminUI 仍无身份隔离；诊断页是功能入口，不是权限边界（ADR 0001）。
- 本机 Chromium/macOS 宿主不代表目标拓扑；未跑真实设备、Linux 后端或生产迁移。

## 下一接入动作

1. 预算/租约/outbox/计量有读路径后按同一"只读自报事实"的方式补入诊断页，不预先占位。
2. 音色候选/试听/校验/发布 UI 状态继续（试听与发布依赖 A06/B05）。
