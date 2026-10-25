---
title: "团队 C C02 正式文件转写入口交接"
status: active
date: 2026-10-24
---

# C02 正式文件转写入口

## 范围与基线

依据 [C02/C08](2026-10-23-three-team-delivery-plan.md#43-c-团队应用与体验)，按[本单元计划](2026-10-24-team-c-c02-host-plan.md)将既有真实客户端/会话/journal 接入正式 Workbench。当前 base/head 为 `dc14cf8fb0a7250842a8a7697c6884d6d21b2004`；fetch 后 origin/main 无新增变更，起始 140 文件均与旧 manifest 摘要一致。未 commit/push/外部消息；完整 C00–C08 goal 保持 active。

消费 A speech `0.1.0.dev0` / contracts package `0.1.0` 公开生成 helper 和 Asset/Job/Event/Artifact/Error validator，不编辑 canonical、生成流水线或产物、依赖/锁、后端/DB/deploy。本轮不重复授予旧 C00/C01/C04 等资格。独立终审的 P2 修复额外修改既有 `file-task-session.ts` 消费者视图状态，不改机器协议。

## 正式行为

- 默认 `/workbench` 构建包含文件转写宿主，开发演练仍受 DEV 分支隔离。使用 `location.origin`，没有浏览器可改的服务端点，也不回退 fake；初载/刷新构造不请求，用户显式读取/上传/重放/连接事件。
- 选择真实 File 和部署模型 ID；模型一经开始即固定。上传使用既有 A helper，TTL/workspace 保持省略语义，部署须显式 retention。音频和文件名不进入 journal，界面提交后清空文件选择。原输入恢复、v5 原 Asset hint、v1 原 Job/Operation 和 create/cancel 原键继续由既有 session/journal 实现。
- 独立 ASR 恢复 key 为 `speechnexus.workbench.asr-task.v1`，只接受严格数字 v1/v3/v5。存储异常、损坏或异类私有记录暂停页面提交，保留原记录，不转内存模式或新键。这里的私有 v1 key 不改变记录内部版本。
- 两步取消显示并固定原任务目标，确认按钮聚焦；若快照对象变更或 unknown/busy，不确认旧目标。取消 accepted 与任务、资源清理分别显示。事件手动连接/停止，保留大整数字符水位；pagehide/hidden 停事件，StrictMode effect 重放不销毁会话，真正卸载 abort 并清理。
- 只下载当前 Job 引用、已提交且经生成校验的原始 JSON 制品，沿用 16 MiB 有界读取；检查 transcript/application-json，文件名固定 `transcript.json`，Blob URL 释放。卸载迟到下载不创建 URL 或点击链接。校对版本和字幕导出不是原始 JSON 下载，本轮仍待其工作台生产接口接入。
- 每标签页保留一个 ASR 意图。任务已终态、cleanup complete、无 busy/unknown 时可两步确认新任务；此外仅首次新上传收到 413 `asset_too_large` 或 422 `asset_empty` 的具名明确拒绝，可在原标签页两步结束输入并更换文件。其他错误、先前未决、恢复记录或 hint 不使用该例外。清除前核对当前本地 owner 字节，失败保留原任务。只结束本页恢复，不删除服务器任务/制品；新建使用新键并聚焦模型输入。
- 沿用 AppShell、共享 Token/中文字体与左输入右详情布局，360px 纵排、长 ID 换行、语义 label/status/alert/group 和可见 focus。未扩张共享组件或重新设计站点外壳。中英 README 同步状态。

A 的 ModelList 类型有公开定义，但 bindings/runtime 尚无生成校验入口；本轮不复制字段或私造 validator。暂由用户显式填写部署已发布的模型标识，可用性由服务端判断。模型发现/就绪及质量资格展示继续依赖 A 发布完整消费入口，手填并非该需求最终完成。

## 验证（2026-10-24，Asia/Shanghai）

- RED：正式 App 缺上传按钮；新增宿主测试先因模块未实现失败。首轮 3 失败/11 通过来自 jsdom 文件 validity 和 synthetic fixture 错 ID；修正测试提交方式/精确 ID/文案后 12 宿主 + 2 App 通过，未放宽 parser。类型/lint 问题限于测试 any/act/URL descriptor 和组件 helper 导出边界，修正后通过。
- 视觉核对后补已保存素材 ID 和新任务模型 focus 两个行为断言，先 2 RED 再 GREEN。owner 比较删除变异使 1 项恢复测试失败，源码逐字节还原后 12 宿主通过；不将变异失败作为正式通过。
- 修复后完整 Web 434（Admin 79、Workbench 234、contracts 121），format/lint/typecheck/build 通过。正式 Workbench JS 约 457 kB / gzip 132 kB，包含实际 validator/客户端；未新增依赖。新探针独立 ESLint/Prettier 检查通过；canonical 28 文件及生成 drift up-to-date；旧 ASR UI 9、TTS UI 4 与 Asset 恢复 10 场景全部通过。
- `node scripts/dev-env/workbench-file-host-probe.mjs --output .runtime/team-c/c02-host-browser-final-fixed.json --screenshot .runtime/team-c/c02-host-desktop.png`：实际 Chromium 156、正式生产 bundle（`productionUI: true`）、loopback 合成 HTTP。真实 multipart 校验 bytes/name/type/purpose 与省略元数据；v5 hint 跨刷新、503→staging→ready 后显式建 Job；创建响应丢失原键重放；两个 SSE 大水位跨刷新；46 字节原始 JSON 下载逐字节及 Blob URL 释放；owner 改写拒绝清理；显式新任务换键；取消响应丢失刷新后原目标/键重放；无 Location 原文件/原上传键；存储不可用无请求。修复后 6 次上传/5 次创建请求/2 次取消请求；新增首次 413 后确认新输入、较小 WAV 成功且新键的正式页面回归。全 handler error 为零；页面错误与出站为零仅限已安装监测的主流程、无 Location 与纠正输入 Context，存储阻断 Context 只核验 API 计数与表单阻断（下述 P3）。
- 键盘检查包括模型到文件 Tab、取消和新任务确认 focus/Enter、新建后模型 focus；360px 无横溢，截图已人工查看。生产 bundle 排除演练文案。图谱未索引且 analysis 无 index_repository，具名源码及覆盖失败按路径核对，不声称穷尽结构。

日志 `.runtime/team-c/c02-host-*`，仅合成字节/元数据。新探针 finally 关闭浏览器与 server，不连接真实语音服务或设备。

## 独立审查与累计交付

完成一次独立只读终审，报告 `.runtime/team-c/c02-host-review-result.md`。审查者独立复跑的是修复前 Workbench 228 / Host 12、typecheck、探针语法和 diff，发现 1 项 P2 与 1 项 P3。主代理按实际影响确认 P2，在唯一一次修复 pass 中完成六个新增回归：413/422 首次拒绝后显式纠正两项；原内存未知/刷新恢复后的 413 不解除未知两项；原输入后来接受时清除拒绝标记；本地保存失败未发送请求后，第一次实际上传明确拒绝仍可纠正。前四项 4 RED→GREEN，后两项各 1 RED→GREEN；最终 Host 18 / Workbench 234，全 Web 434。未派第二次审查，不把原审查报告描述为修复后独立背书。

修复仅把首次新上传的两个具名拒绝作为本地结束资格，先前 unknown 或恢复过的上传收到后续 4xx 仍保留未知。理由：A ingress 可能在查原 command 之前按新限额返回 413，这不证明旧 writer 已停止。明确拒绝标记不新增 journal 版本，刷新后仍按原记录核对，不能推断旧请求未生效。后续如果原输入显式重试被接受，立即清除此标记。

暂缓 P3：浏览器探针的存储阻断 Context 未接入资源路由与 pageerror 监听。该分支确实验证 API 计数未增加、表单不可提交；报告的零出站/零页面错误仅适用于其他已监测 Context，不能推广到该分支。未修该监测项，不据此授予完整网络/错误资格。

累计 152 文件，旧 140 文件中 137 个逐字节不变，3 个声明范围内修改（索引、goal 和 session），12 个路径加入累计包，包含首次纳入包的既有 App/测试与中英 README。保存旧 140 文件包，最终 patch / manifest 在 `.runtime/team-c/team-c-current.patch` / `team-c-current.manifest.json`。临时 index、干净 archive apply、152 文件逐字节、225 个本地链接及真实 index 不变均另由主代理核验，摘要以 manifest 为准。中英 README 仅状态/入口更新，保留原文档索引表的既有排版，未顺手格式化。

审查未授予的资格与成本逐项保留：

1. 累计包由主代理独立核验；成本是审查者不为 152 文件整体正确性背书。
2. 完整 C00–C08 与 G0–G5 不关闭；成本是所有余项和真实验收继续推进。
3. 真实模型/GPU/设备、inspector/worker/stop-release/数据协调仍由 A/B 联调；成本是本轮只能证明合成消费者。
4. 真实反代、出站、安全、断网/崩溃/节点及长稳未授予；成本是部署必须另取运行证据。
5. 手填模型与本 ASR 宿主不替代模型发现、正式 TTS/校对/导出及其他业务；成本是这些消费者仍待实现。
6. 只保证同标签元数据与已知 owner 检查；成本是不承诺多标签/跨进程 CAS 或长期持久化。
7. 仅 Chromium 已测键盘/尺寸场景；成本是辅助技术、跨浏览器、所有小窗口状态与大文件性能仍需验证。
8. 具名源码结论不穷尽结构、安全或依赖；成本是未来集成仍需对应范围检查。
9. P2 修复由主代理 TDD 与浏览器证明；成本是没有第二次独立审查，原报告的 P2 保留为历史发现。

## 继续接力

正式文件上传/恢复/下载消费者已接入，不证明完整 C02。A 的实际音频 inspector、Job/worker 和 B 的模型/GPU/stop/release 资格仍待联调；数据库/对象幂等和维护收敛沿用服务团队的真实证据，合成 HTTP 不代替。模型发现生成入口、正式 TTS 与声音/参数选择、校对/导出、项目业务宿主、真实网络与设备、辅助技术和全浏览器资格均未完成。sessionStorage 仅同标签元数据及已知 owner 检查，不承诺跨进程 CAS/多标签一致、关页/崩溃/长期保存。未完成项继续留在 C00–C08 goal，不关闭 G0–G5。
