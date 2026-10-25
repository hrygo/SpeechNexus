---
title: "团队 C C01 Admin 同标签页恢复交接"
status: active
date: 2026-10-24
---

# C01 Admin 同标签页恢复交接

## 范围与基线

本单元补齐正式 Admin 任务中心的刷新恢复，继续执行 [C00–C08 goal](2026-10-24-team-c-goal.md)，不关闭 C01/C08 或 G0–G5。前置为[事件与入口单元](2026-10-24-team-c-c01-events-handoff.md)，实施见[恢复计划](2026-10-24-team-c-c01-recovery-plan.md)。

开始基线 `3380a76c89998b42be29b0c25e9f49443d23e15b`；本轮 fetch 后无冲突快进至 `330d0d70c5d40c03efcb9cb1ed146a97da53d46d`（#61 素材 HTTP、#62 网关屏障、#63 claim fencing），既有 128 个 C 文件全部字节一致。当前 `base_sha` / `head_sha` 均为后者；未创建 commit、push 或发送外部消息。

消费该 SHA 下 A 的 speech `0.1.0.dev0` 生成客户端与 `Job` / `JobCancellation` / `JobEvent` / `ErrorResponse` validator。上游素材接口及生成产物更新已拉取，本单元未修改 canonical contracts、生成器、生成产物、lockfile、后端、数据库或部署组合根。

## 已实现

- 生产宿主使用同源 `sessionStorage` 私有 `speechnexus.admin.task-recovery.v1` 单记录，最多 2048 字符；仅 `version`、`selectedId`、字符串 `cursor` 与 pending 的 `{jobId,key}`。无正文、快照、响应、端点或凭据。严格字段/私有标识边界/UUID/水位校验，未知版本及损坏记录不覆盖。读取结果及嵌套取消记录不可变。
- 取消前同步保存原任务及原键；提交中离开或收到不可解析/丢失响应后，重建会话显示 unknown。用户显式按原键重放，选中另一任务也不改变原目标。收到匹配原目标的响应并清除 pending 成功后才显示 received；清除失败仍 unknown。accepted、任务终态和资源释放继续分别核对。
- 保存当前任务标识与已确认事件水位；仅完整 Job 重读、校验与持久化都成功后推进。精确 410 `event_history_expired` 先保存清水位，再重读当前任务；失败不伪报恢复。切换任务重置所选水位，另一任务 pending 取消独立保留。
- 刷新仅恢复标识、水位与 unknown，不恢复快照或自动 GET/POST/SSE；必须显式重读后才能连接事件。界面显示恢复范围、待核对标识及存储故障。
- 存储读取/写入失败、配额拒绝、损坏或已知 owner 改写后，生产会话阻止取消及事件推进，仍允许读取；不自动覆盖、不降级为可取消内存模式。修复存储后刷新核对原记录。
- 旧开发 fake 和未注入 journal 的 session 明确保持本页内存行为。StrictMode 和原有生命周期清理沿用已验证路径。每次更新会比较上次读取的原始存储值；这不是跨进程事务/CAS。

## 实测证据（2026-10-24，Asia/Shanghai）

- TDD：journal 缺失模块 RED→15 通过；session 新增 15 项 RED→GREEN；界面新增 2 项 RED→GREEN。当前 Admin 共 79 项通过。移除取消前保存失败的阻断条件时 2 项失败、13 项通过；恢复原字节后 15 项再次通过。
- 完整 `pnpm format:check` / `pnpm lint` / `pnpm typecheck` / `pnpm test` / `pnpm build` 通过：**379 项（Admin 79、Workbench 179、contracts 121），0 skipped**。Admin JS 445.37 kB / gzip 128.52 kB。新/改浏览器探针单独 Prettier/ESLint 通过。
- 依照上游 lockfile 执行 `uv sync --locked --all-packages --all-groups`；`make contracts-check` 28 文件通过，`uv run python scripts/dev-env/generate-contract-bindings.py --check` up-to-date。生成器对临时输出应用既定格式修复，不改当前源码。
- `node scripts/dev-env/admin-task-recovery-probe.mjs --output .runtime/team-c/c01-recovery-probe-final.json --screenshot .runtime/team-c/c01-recovery-probe-final.png`：实际 Chromium 156 + 正式生产 bundle + 测试专用 loopback HTTP，覆盖不可读取消响应后刷新/切换任务/原键重放、提交中刷新、ack 保存失败后 unknown、拒绝写入不 POST、其他 owner 改写不覆盖、损坏记录保留、初始拒绝读取不崩溃、大水位刷新续读及 410 清水位再刷新。键盘确认、320px 亮暗无横向溢出、0 pageerror、0 非 loopback 请求。报告只输出布尔值/计数，不输出键或内容。
- 原 Admin 事件探针 6 HTTP + 4 开发场景通过；现为每个独立合成场景清理该测试上下文的恢复记录，刷新能力由新探针单独验证。原 Admin 任务探针 5 场景、键盘、取消原键/原目标与生产排除 fake 回归通过。
- 初始 test-only lint 4 项已修复，无禁用规则。一次修正测试时缺括号导致构建失败；误运行旧 dist 的首轮浏览器结果不计通过。该轮 socket destroy 曾观察到浏览器原生传输重复 POST，因此最终探针使用服务端已接收但响应 JSON 不完整的场景，另测真实 in-flight 刷新；不声称浏览器传输层绝不重复请求，原幂等键始终是安全边界。

日志位于本地忽略目录 `.runtime/team-c/c01-recovery-*`，测试均使用合成元数据；无私人媒体、真实模型、GPU 或外部部署访问。

## 验证边界与接力

- 仅保证同 origin 同标签页 sessionStorage 正常工作的刷新恢复；关闭标签页、清除站点数据、崩溃恢复、跨机器不承诺。浏览器本地记录不是登录身份，也不提供服务端数据隔离。
- 真实 A04、代理/TLS/出站限制、Safari、BFCache、长期后台/负载及真实设备仍需分别验证；本单元未运行 Python/PG/MinIO/GPU 集成测试，其旧证据不扩大。
- SpeechNexus 图谱仍未索引，当前 analysis 工具不提供 index_repository；对具名文件直接源码审查，不能声称结构覆盖完整。
- 已知存储故障保持阻断，用户需保留原记录并修复后刷新；没有“清除未知取消并换键重试”的快捷路径。
- 团队 A/B 资格、正式语音 worker/完整工作台业务、C03/C05/C06/C07 及其他 C08 条目仍未完成。

## 独立审查与累计交付

独立只读终审 `/root/review_c01_recovery` 已完成：当前 13 文件单元可接受，无高置信 P1/P2、无新增 P3。审查者独立 Admin 79、Admin typecheck、diff 检查通过，并核对 13 路径/29 hunks 与关键实现摘要。没有修改源码或启动服务。浏览器/完整套件为父代理实测，独立审查没有重跑它们。

审查明确不评判最终累计包（由父代理另核验）、真实服务/部署、跨进程 CAS/崩溃持久性、旧工作包及全图/安全/负载/辅助技术完整性；均按上述证据范围保留，不借测试扩张完成声明。先前 Admin 开发探针 all-HTTP 与文本下载 Accept-Ranges oracle 两项 P3 仍在原单元延后，本轮未新增 minor。

累计交付为 135 个文件：新增 7、修改原包中的 6、保留原包另 122 个文件字节不变。补丁 `.runtime/team-c/team-c-current.patch` 与同名 `.manifest.json` 保存确定基线、逐文件 SHA256 与补丁摘要；旧 128 文件事件包保存在 `.runtime/team-c/team-c-c01-events-3380a76.patch` 和对应 manifest。父代理以临时 Git index 生成补丁，在干净 `git archive HEAD` 中 apply-check/apply 并逐文件比对，另检查文档本地链接及真实暂存区保持空。目标继续 active。
