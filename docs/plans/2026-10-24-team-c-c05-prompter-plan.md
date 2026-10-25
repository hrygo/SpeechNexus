# C05 提词跟随与手动阅读计划

> 执行：沿用已授权 native / executing-plans；TDD 后一次独立只读终审。不提交或推送。

**Goal:** 固定本地阅读稿版本，提供有限窗口跟随核心、手动定位/恢复点和显式浏览器保存；正式工作台可使用手动阅读。

**Architecture:** 独立 TypeScript 领域状态机消费带固定稿件身份、epoch 和 generation 的内部 hypothesis，不解析公开 wire；React 仅提供已可用的手动阅读。后台/离开使跟随上下文失效。显式保存本地快照，恢复始终手动。

**Tech Stack:** 现有 TypeScript / React / Vitest / Chromium，不加依赖。

**Spec:** [产品 §9.2](../speechnexus-product-architecture.md#92-提词与助手)、[三队 C05](2026-10-23-three-team-delivery-plan.md#43-c-团队应用与体验)。自动实时接线、服务端持久化、真实设备及完整 C05 验收继续保留。

## Global Constraints

- base/head dc14cf8；保留累计165文件，生成契约、锁、后端、公共组合根和README不改。
- 内部本地稿件身份不冒充服务端 Transcript revision；不请求麦克风或网络，不假装自动跟随可用。
- 使用既有设计 Token：正文阅读居中窄栏、保留换行、大字号；控制左对齐、可换行、44px触控和可见焦点；无外部字体或动画。
- 原稿只在显式“使用此稿”时替换；保存按钮才写入本地存储。失败不宣称保存成功，恢复校验有界结构，始终手动。

## Task 1：跟随核心与恢复快照

Files: 新增 `web/workbench/src/teleprompter/follow-session.ts` / `.test.ts`。

Interfaces: `FollowSession(script)`、`beginFollowing(epoch)` 返回固定 `FollowContext`；`acceptHypothesis(context,{sequence,text})` 单调水位；`seek(position)` / `pause()` 退休 generation，`restoreResumePoint()` 手动回到接管前；`checkpoint()` / `restoreCheckpoint(raw)` 独立保存精确正文和 codepoint 位置，恢复无活动 epoch。

- [x] RED：固定身份/epoch/generation，旧序号/旧稿/旧epoch、手动/暂停/恢复后迟到结果均不推进；有限窗口、重复歧义、低信息、错识别、修订回退与 Unicode；非法/超限输入和快照拒绝。
- [x] GREEN：前后各160 codepoints 内完整词元唯一精确匹配，仅向前；最少4有效字符，hypothesis≤256、script≤20000 codepoints。检查 source text 不变，快照无活动会话身份。
- [x] Run `pnpm --filter @speechnexus/workbench exec vitest run src/teleprompter/follow-session.test.ts`；Expected 全通过。

## Task 2：正式手动入口与浏览器验证

Files: 新增 `TeleprompterPanel.tsx` / `.test.tsx`、`teleprompter.css`、`scripts/dev-env/workbench-prompter-probe.mjs`；修改 `App.tsx` / `App.test.tsx`、目标与文档索引；新增交接。

- [x] RED→GREEN：空稿提示、固定稿/编辑草稿分离、键盘/范围定位、接管前恢复点、显式保存与恢复、存储失败/损坏拒绝、HTML文本安全、切换离开清理。正式入口显示自动跟随待接入。
- [x] Run format/lint/typecheck/test/build；Expected 全通过，已有 ASR/TTS 不回归。
- [x] 生产 Chromium：本地稿Unicode/换行、键盘定位、显式保存/刷新恢复、切换不自动保存、无网络/麦克风调用、360px与可见焦点；关闭browser/server。Expected 实测证据，不扩大为语音/设备资格。
- [x] 独立只读终审：Critical/Important/Minor均0，无需修复；更新交接和累计包，干净apply/hash/链接/index检查，goal保持active。

## Review Focus

1. 词元规范化只用于匹配，原正文/位置映射不可变；重复句和长文本不越窗。
2. 旧 callback 在手动定位、暂停、同epoch重启或换稿后无效。
3. 本地存储不自动保存正文、不保存活动epoch，不暗示服务端持久性或身份隔离。
4. 用户编辑输入、键盘操作和关闭页面不会被跟随逻辑夺回控制。
5. 当前正式入口仅提供真实可用的手动阅读，不呈现虚构实时成功状态。
