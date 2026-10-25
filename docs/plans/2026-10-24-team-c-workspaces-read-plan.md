# Admin 工作空间只读消费者实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans 在当前会话内联逐项实施；整单元一次 fresh 终审。保留工作树/ignored证据，不自动提交、推送或清理。

**Goal:** AdminUI通过同源公开GET读取工作空间列表/详情与分页，明确共享部署和历史快照，覆盖错误/迟到/隐藏，不构造身份或管理写路径。

**Architecture:** 先安全集成A #90–#93至2a46c6e，再消费A生成admin/admin-http的两个GET。client负责生成边界与身份/分页可用性；session负责取消/代次/当前状态；host只展示字段并接受显式动作。没有浏览器持久化、轮询或默认Workspace。

**Tech Stack:** 现有React/TypeScript/Vitest/Playwright和同版生成parser，现有Web Token；不改依赖/生成器/公共协议。

**Spec:** [产品设计](../speechnexus-product-architecture.md) §共享部署/工作空间；[三队计划](2026-10-23-three-team-delivery-plan.md) C01/C08；`contracts/admin-openapi.yaml` Workspace/WorkspaceList及listWorkspaces/getWorkspace；A #93只交付生成契约，实际PG/GET装配尚待合入。

## Global Constraints

- 无登录/角色/用户隔离；workspace只是内容与预算分组。禁用状态不是访问权限证明。
- 只消费生成GET，默认query保持省略，不暴露计划中的POST/PATCH或虚构配额/模型/用量。
- 当前部署同源、redirect:error、cache:no-store、15s期限、1MiB JSON上界；只显示稳定错误code。
- 显式读取/分页，加载失败保留标记历史列表；切换选择不显示其他对象详情，隐藏/卸载使迟到结果失效。
- 上游TTS/双库/CI报告保留来源，合成浏览器不授真实生产/设备/Gate资格。

## Review Focus

1. 生成GET选择不能意外提供Workspace创建/修改；省略默认query且不把workspace当身份。
2. 页游标循环/重复id、分页失败后旧列表与下一页资格，不混页或自动请求。
3. 快速切换详情/刷新列表/隐藏再显示后旧HTTP响应不得覆盖新选择。
4. 404/503/非法成功DTO或身份不符不能保留冒充当前的旧详情、泄漏服务端message或自动重试。
5. StrictMode清理及真实导航/pagehide停止，返回仅显式读取；360px与基本键盘可用。

### Task 1: 生成读取边界

**Files:** 新建 `web/admin/src/workspaces/workspace-client.ts`、`.test.ts`。
**Interfaces:** `Workspace/WorkspaceList`从生成Contracts派生；`WorkspaceApi.list(cursor?, signal?)/get(id,signal?)`；`WorkspaceClient(baseUrl,fetch?)`实现。

- [x] RED：精确GET/省略默认query、生成拒绝非法id零fetch、同源/redirect/期限、稳定错误、错id/非法DTO、重复id和无进展cursor拒绝。
- [x] GREEN：消费生成createValidatedClient/parse，1MiB上界；无独立wire副本。
- [x] 验证 `pnpm --filter @speechnexus/admin exec vitest run src/workspaces/workspace-client.test.ts` 全部通过。

### Task 2: 只读会话

**Files:** 新建 `workspace-session.ts`、`.test.ts`。
**Interfaces:** `WorkspaceSession(api)`，`snapshot/subscribe/list/get/retry/setActive/dispose`，返回冻结快照；Task3仅订阅此状态。

- [x] RED：首次不自动GET、分页失败保留历史/重试原cursor、游标周期拒绝、快切详情/列表及隐藏后迟到结果、失败详情清空、dispose终止，禁止自动重试。
- [x] GREEN：单活跃AbortController/epoch；明确page/detail当前标志及请求中的目标，外部响应再做生成/上下文验证。
- [x] 验证 `pnpm --filter @speechnexus/admin exec vitest run src/workspaces/workspace-session.test.ts` 全部通过。

### Task 3: 正式入口和交付

**Files:** 新建 `WorkspaceHost.tsx`、`.test.tsx`、`workspace.css`、`scripts/dev-env/admin-workspaces-probe.mjs`；修改Admin App/测试、双语README、计划索引/Goal；新增本单元handoff。

- [x] RED/GREEN：正式页面显式GET、共享文案/无写入口、合法空列表、状态/版本、分页及错误/历史、焦点/隐藏恢复；App不引入生产fake。
- [x] 生产bundle Chromium通过合成生成HTTP列表/下一页/详情、失败/迟到/隐藏/显式重读、无写请求/出站和360px基本键盘；同版既有C03/ASR/TTS/真实C HTTP隔离PG浏览器回归。
- [x] Web五门禁/28契约漂移，集成后Python完整unit/contract及隔离PG；一次fresh终审后Critical/Important一轮TDD修复，Minor延期。
- [x] 含新增文件累计补丁、干净apply/全字节/链接/index保护；九issue状态及任务更新逐字回读。完整C00–C08 Goal保持active。
