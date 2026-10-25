# 工作台删除预检、确认与恢复 Implementation Plan

> 执行：沿用已授权的 executing-plans inline 模式；实施由主代理完成，整个单元末尾一次 fresh review。不提交或推送。

**Goal:** 让共享工作台使用者根据真实目录、固定版本和完整依赖范围确认删除，并恢复未知结果、读取当前清理事实。

**Architecture:** 扩展现有同源 WorkbenchClient 消费三个冻结操作；独立 journal 验证持久请求和关联，session 管理显式操作及迟到隔离，React 页面展示服务端事实。已访问页面保留内存，sessionStorage 损坏或写失败时阻断提交。

**Tech Stack:** 现有 React/TypeScript、生成 workbench client/validators、Vitest、生产bundle Chromium、一次性socket-only PostgreSQL。

**Spec:** [删除规范](../architecture/data-deletion.md)、[源契约](../../contracts/shared/deletion.schema.json)、[HTTP契约](../../contracts/workbench-openapi.yaml)、C00 #35 / C08 #43 当前任务。

## Global Constraints

- 仅支持C已实现的workbench project/transcript预检，依赖可含meeting；固定默认workspace，同源、禁止重定向，不新增协议/依赖或手改生成产物。
- 完整确认集合等于目标加全部cascade依赖，排除retain与blocks；独立副本不能级联。展示owner/kind/id/revision、plan_revision/digest/有效期。
- 原键/原body先持久化再POST；未知结果不生成替代意图。原202接受快照与GET当前清理事实分开，恢复不表示撤销。
- 保留所有267既有文件和他人改动；只写本单元具名文件。生产迁移/组合根由A串行接入。
- 所有测试数据合成；无模型、设备、生产删除、跨chat、commit/push。既有六Minor延期。

## Review Focus

1. 计划中两个不同关系指向同资源：确认集合去重，但每条依赖完整展示；不漏目标或夹带retain。
2. HTTP提交成功后回执丢失/保存ack失败：原意图保留；重放不新建删除，已接受不伪装清理完成。
3. 过期/依赖变更/阻断及schema合法但身份错误响应：拒绝写入成功状态，重新预检必须显式操作。
4. sessionStorage损坏/quota/外部改写、功能导航和卸载：提交屏障、内存保留、无迟到发布或跨意图覆盖。
5. 共享部署中保留副本、错误正文、长ID/窄屏/键盘：范围可审阅、文本安全、稳定无内容错误，基础键盘及无横向溢出。

## Task 1: 冻结客户端与恢复记录

Files: 修改 `web/workbench/src/manuscripts/workbench-client.ts`；新增 `web/workbench/src/deletions/deletion-{plan,journal}.ts` 和对应测试。

Interfaces: `previewDeletion(body,key)` / `acceptDeletion(body,key)` / `deletion(id,signal?)`；`confirmation(plan)` 由生成类型构造完整唯一集合；`DeletionJournal.load/save` 保存闭合状态及请求关联。

- [x] 先测试三个操作、完整/重复关系/retain、错误身份、未知字段、跨plan请求及存储失败，运行应先失败。
- [x] 实现生成边界与关联校验、CAS式storage写入；运行 focused tests，Expected: 全通过。

## Task 2: 会话与正式页面

Files: 新增 `deletion-session.ts` / `DeletionHost.tsx` / `deletions.css` 及测试，修改 `App.tsx`。

Interfaces: session消耗Task1冻结请求；公开显式目录/选择/预检/确认/replay/refresh/reset，external-store snapshot只读。App延迟加载并在功能导航间保留已访问host。

- [x] 先写成功后丢响应、409/过期、ack保存失败、读取失败/迟到及确认交互测试；Expected: RED。
- [x] 实现目录分页、显式mode与逐项确认、原键恢复、当前状态/独立副本与storage屏障；Expected: focused GREEN。
- [x] `pnpm format:check` / `lint` / `typecheck` / `test` / `build`，Expected: 全通过；不改变既有Meeting行为。

## Task 3: 实际浏览器、审查和交接

Files: 新增浏览器probe和隔离PG集成测试；按需扩展现有显式browser探针列表；更新本计划、交接、索引与Goal指针。

- [x] 生产bundle→真实C HTTP→私有PG覆盖普通阻断/级联完整确认、两次实际提交后丢回执原键、刷新恢复、独立副本与实际物理清理、360px/基础focus及出站0。
- [x] 受影响Python静态/测试及Web门禁；既有未改源码证据可复用。Expected: 覆盖范围明确、无skip冒充通过、私有PG停止。
- [x] 一次fresh终审，只修Critical/Important一轮且各自RED→GREEN；Minor与Declined逐条裁决。
- [x] 累计包干净base apply/全部字节/链接/index保护，issue状态/任务与最终回读；完整Goal与Gates保持未完成。
