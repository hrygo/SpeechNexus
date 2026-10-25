# C1 项目与稿件版本工作台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans inline，唯一一次fresh final review；不commit/push，不清理既有产物。

**Goal:** 在正式工作台发现/创建项目与稿件，读取固定历史版本并追加校对，保留本地草稿、冲突与未知提交的原意图。

**Architecture:** 以既有生成C1 HTTP client为边界，独立client、sessionStorage恢复记录与外部状态session；React宿主订阅session并接入现有App导航。页面只使用同源公开协议，不导入底座内部，不把本地缓存当服务端事实。

**Tech Stack:** 既有React/TypeScript/Vitest、A createValidatedClient/parse、项目Token；生产bundle的Chromium合成HTTP探针。

**Spec:** `contracts/workbench-openapi.yaml`，`docs/speechnexus-product-architecture.md` §2.2，`docs/architecture/ui-ux-design-framework-and-tokens.md` §3；C00 #35/C02 #37/C08 #43及完整Goal。

## Global Constraints

- base/head `3b8052040111cdd8af3a50aaf5b0e8b3922e1f42`；开工逐字节核对225文件补丁b6530817。#35/#37/#43状态与任务开工前已更新回读。#77仍open/head9a913ae，不吸收未合并制品。
- 只写C-owned前端、测试/探针和交接。A生产root/迁移、生成器与共享锁不改；无commit/push/设备/模型/GPU/新跨chat消息。
- wire类型/请求/响应验证均来自既有生成入口；公共默认workspace/limit由服务端/契约决定，不复制默认值。首轮页面消费部署默认workspace，workspace切换不在本单元虚构。
- 只允许location.origin同源API，redirect:error、不自动retry、不缓存HTTP正文；单JSON响应本地限界8MiB不代表服务端协议上限或整站容量。
- 项目/稿件共享部署标签沿用AppShell；不增加身份、个人权限或虚构能力。配置status、空列表、禁用/503与故障真实呈现。
- 稿件正文不trim/normalize，原稿sealed不可覆盖；修订传精确expected_revision与source_revision字符串，不转Number。历史读取显式revision，不伪造历史列表。
- 所有mutation先持久化原键/原路径/原请求，再发送；未知网络/5xx/无效响应保留pending，只有用户显式原键重放。刷新/重挂载不自动发送mutation。
- CAS冲突保留草稿，读取最新作为比较；用户显式基于最新继续才产生新key/新expected_revision。不得静默覆盖草稿或自动重试。
- sessionStorage仅保留本标签页恢复所需草稿/原意图，不输出正文/完整请求到日志；损坏/CAS归属变化/配额失败须阻断提交，保留原记录与内存草稿。
- 切换选择有未保存草稿须明确舍弃；pending禁止放弃/替代意图。读取取消/迟到与卸载不代表服务端mutation回滚。
- 布局沿用现有Token：桌面项目/稿件目录与文稿两栏，内容左对齐、正文宽度约72ch；窄窗口堆叠，清晰键盘焦点/状态/错误，不引入外网资源或额外视觉主题。

## Review Focus

1. 刷新/StrictMode/卸载时请求在途，已提交但响应丢失，原键与原正文必须存续且不自动重发。
2. 冲突/历史校对/另一个编辑者追加时，不丢草稿、不用旧正文冒充最新、不更改原稿或伪造合并。
3. 存储损坏/写失败/归属变化、伪造跨资源响应或错误分页，不能发新mutation或切换到错误文稿。
4. 快速切换/重复点击/迟到读取，当前文稿与所属项目一致，pending不产生替代key。
5. 空部署/服务关闭/缺少资源、纯键盘与窄屏；生产入口按实际事实提示，不把浏览器合成HTTP当真实部署资格。

### Task 1: 生成客户端与恢复记录

**Files:** `web/workbench/src/manuscripts/workbench-client.ts`、`workbench-client.test.ts`、`editor-journal.ts`、`editor-journal.test.ts`。

**Interfaces:** `WorkbenchClient`消费createValidatedClient/Contracts，提供status/projects/project/createProject/transcripts/createTranscript/view/revise；`EditorJournal`只存version1的form/view/draft/pending，load/save带原raw CAS，wire子对象调用生成parse与validateRequest。

- [x] 写同源/生成拒绝/错误脱敏/无重试/identity/精确revision/list scope用例，先RED，实施client并GREEN。
- [x] 写恢复closed shape/生成wire校验、不可变请求、存储CAS/损坏/失败不覆盖用例，先RED，实施journal并GREEN。

### Task 2: 草稿与版本状态

**Files:** `editor-session.ts`、`editor-session.test.ts`。

**Interfaces:** `EditorSession`提供subscribe/getSnapshot、discover/load-more/select、edit/create/save/replay/rebase/discard、retryStorage/dispose；依赖WorkbenchClient和EditorJournal，快照保持引用稳定供useSyncExternalStore。

- [x] 测试发现/分页/创建/原稿与历史修订、CAS冲突/显式重建基线、未知提交刷新原键重放、存储故障和迟到结果，观察RED。
- [x] 实施单写入状态与持久化屏障；用户动作控制mutation，读取可取消，卸载不写恢复记录；GREEN。

### Task 3: 正式页面与浏览器交接

**Files:** `ManuscriptHost.tsx`、`ManuscriptHost.test.tsx`、`manuscripts.css`；修改`App.tsx`/`App.test.tsx`；新增`scripts/dev-env/workbench-manuscripts-probe.mjs`及handoff，更新goal/index/README双语。

- [x] React/StrictMode集成先RED：正式导航、共享边界、状态/空/错误、项目/稿件创建与分页、历史只读/追加修订、冲突比较、草稿/unknown刷新与明确舍弃。
- [x] 实施两栏文稿体验及窄屏/键盘/焦点，组件GREEN；不加载fixtures或自动提交，浏览器验证通过。
- [x] 五项Web门禁及受影响契约检查，生产bundle真实Chromium合成HTTP验证/截图；一次独立终审0/2/0，一轮Important必要TDD修复53项/Web940通过，记录所有裁决。
- [x] 累计包干净base apply/全部字节/链接/index保护；更新相关issue与任务并回读，完整Goal/Gate保持未完成。证据见[交接](2026-10-24-team-c-c1-editor-handoff.md)及忽略目录的最终manifest。

删除页面、公开导出冻结、底座Artifact导入页面及Meeting/Minutes/realtime分别继续实施；此单元不改变完整Goal的退出条件。
