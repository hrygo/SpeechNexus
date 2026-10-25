# C1 项目与稿件版本工作台交接

核验日期：2026-10-24。base_sha=head_sha 为 `3b8052040111cdd8af3a50aaf5b0e8b3922e1f42`；没有 C commit/push。本单元是既有 C1 HTTP 的正式页面消费者；[完整 Goal](2026-10-24-team-c-goal.md)及 C00–C08、G0–G5 均未因此完成。步骤见[实施计划](2026-10-24-team-c-c1-editor-plan.md)，生产者沿用[C1 管理 HTTP 交接](2026-10-24-team-c-c1-management-handoff.md)。

## 实现

- 正式工作台增加“项目与稿件”入口：实际配置状态、项目/稿件列表及分页、创建项目和手工原稿、固定历史版本读取、追加校对。原稿 sealed，不覆盖历史；revision 全程保留精确字符串。
- `web/workbench/src/manuscripts/` 分为生成客户端、恢复记录、状态 session 和 React 宿主。所有 wire 请求/响应使用 A 既有生成类型、client 和 validator，契约仍为 `0.1.0.dev0`；未改生成器、公共协议、共享锁或 A 生产组合根。
- API 固定 `location.origin`，禁止重定向、HTTP 正文缓存和应用自动重试；单 JSON 响应限界 8MiB。默认 workspace/limit 交由服务端和生成契约，不伪造 workspace 发现入口或身份隔离。
- 每次写请求先在本标签页 `sessionStorage` 保存 UUID、原路径与原请求。结果未知保留 pending；刷新/StrictMode 不自动写入，用户显式按原键重放。正文精确保留 Unicode、空白，不输出到日志。
- 冲突保留草稿并展示最新版本；用户显式继续才采用新基线。损坏、配额失败和另一写入者改变恢复记录时阻断提交，保留旧 durable 记录及内存草稿供复制。切换未保存内容需明确舍弃，pending 不允许替代意图。
- 两栏目录/文稿沿用项目 Token，窄窗口堆叠；键盘创建、舍弃与焦点恢复有测试。禁用、空部署、错误及历史只读均使用实际状态，不填充虚构资源。

## 一次终审与修复

唯一独立终审发现 Critical0/Important2/Minor0，审查前后12个冻结源码 hash 一致，独立50项通过。两项 Important 均在一轮修复中先观察失败再修复：

1. 稿件 A 的旧冲突不能在选择 B 时因本地写失败残留。恢复记录和关联状态原子发布；rebase 额外核对项目、稿件及当前版本身份。配额失败、重试保存、rebase 后实际写入仍指向 B。
2. 提交 r2 后回读若 head 已到 r3，不自动采用 r3 为已接受基线。保留 r2 的正文/基线，读取 r3 进入冲突；普通提交和 unknown 原键重放两条路径均要求显式继续，刷新也保持该屏障。

新增3项回归先得到3 failed/22 passed，修复后针对性53项通过；无第二次审查。当前单元没有新增 deferred Minor；上一管理 HTTP 单元的两个文案/细分错误码 Minor 保留原记录，未顺手修改。

## 验证与制品

- Web 五门禁：`pnpm format:check`、`pnpm lint`、`pnpm typecheck`、`pnpm test`、`pnpm build`。最终结果及执行输出位于忽略目录 `.runtime/team-c/c1-editor-3b8052/`；完整测试为940项（contracts526、Admin79、Workbench335）。
- 生产 bundle 探针：`node scripts/dev-env/workbench-manuscripts-probe.mjs --output .runtime/team-c/c1-editor-3b8052/browser-postfix.json --screenshot .runtime/team-c/c1-editor-3b8052/editor-postfix.png`。实际 Chromium `156.0.8078.4`，4个隔离上下文、7次写请求；覆盖提交后丢失回执/显式原键重放、冲突/历史校对、草稿刷新、键盘舍弃、创建、损坏/配额失败/禁用部署及360px无横向溢出。出站、页面异常和生成契约处理错误均为0；finally 关闭 browser/preview。
- 丢失回执由同源 `route.fetch` 确认合成服务提交后 `route.abort` 注入，不把该场景等同真实数据库 COMMIT ACK 丢失。早期探针 socket.destroy 不稳定及 about:blank storage 初始化错误均只修测试装置；未放宽产品网络/存储权限。
- 本单元未改 Python、机器契约或后端；上一单元 Python2414、隔离 socket-only PG269及28源契约/drift通过证据复用，不能称本轮重跑或新的生产资格。
- 累计补丁/manifest：`.runtime/team-c/team-c-current.patch`、`.runtime/team-c/team-c-current.manifest.json`；包含237个文件，SHA-256以最终 manifest 为准。由干净3b基线 apply-check/apply，逐文件核对字节、本地文档链接及真实 index 不变。上一225文件 b6530817 包保存在 `.runtime/team-c/team-c-c1-management-before-editor.*`；没有删除旧交付物。

## 边界与下一接收方

A 仍需串行接生产 root/owner 迁移及真实服务；C 继续删除页面、公开下载契约、Artifact 导入、Meeting/Minutes/realtime、独立保存/导出和 C03 等完整业务。收尾时核实 [PR #77](https://github.com/hrygo/SpeechNexus/pull/77) 已于2026-10-24 02:48:37 UTC合并，merge commit为 `d7156943b08cea5111ea383ff6c42c0a7cd15d34`；本轮固定3b基线未吸收，下一单元先核对当前main和冲突后集成，不再将C2标为未交付。上述 Chromium 是合成 HTTP 消费者证据，未验证真实后端/MinIO/双库协调、物理设备、模型质量、GPU、Linux 部署、真实容量或部署出站限制。客户端同源约束不能代替网络边界资格。

完整 Goal active；issue #35–#43 的父任务和真实资格只按实际覆盖勾选。依赖未合并制品不擅自吸收；本轮没有新跨 chat 消息。回退为保留旧225文件补丁及当前未提交增量，交付未接入生产运行态。
