---
title: "部署单元规划审查：松耦合与跨平台"
status: active
date: 2026-10-26
---

# 部署单元规划审查：松耦合与跨平台

审查目标由用户明确为“松耦合，尽量跨平台”。审查对象为
[部署单元规划](../architecture/deployment-units.md)及
[ADR 0011](../decisions/0011-distributed-deployment-and-platform-boundaries.md)，
源码基线为 `96d2e55b3e7da1faac656cfcfcab1c1c49f5eda5`。
本报告保留 2026-10-26 的审查事实与取证范围；审查时规划 v0.2.0、ADR 修订和本报告
尚在工作区，未提交或集成。当前维护的拓扑图与扩容说明见部署单元规划 §2。

**审查结论：部署方向符合目标，需落实四项实现/取证缺口才能取得对应运行资格。**
本轮修订消除“角色都要服务化”和“节点组件必然是 GPU 代码”的歧义；不批准任何产品 Gate。
既有多机、停止/释放、预算、内容网络和用户放行要求继续有效。

## 1. 审查原则

- 应用通过固定契约、显式配置与持久事实协作，业务代码不绑定本机 GPU、容器 daemon 或 Linux 内核。
- 包、进程、部署单元和主机分别描述；允许共享基础制品或声明后台托管，避免为每个角色增加网络服务。
- 首批应用原生资格为 macOS、Linux；Windows 等保持未验证。HTTP 协议可移植不等于整套实现已支持所有操作系统。
- 保留为一致性与安全必需的耦合：完整准入的 PG 事务、真实节点/generation 归属、入口屏障及停止/释放证据。

## 2. 发现与处理

### R1 · P1：独立 Workbench 服务尚未进入默认部署装配

[Web 代理](../../deploy/web/nginx.conf)把 `/v2/workbench/` 发往 `api:8000`，
[Compose](../../deploy/compose.yaml)没有 Workbench API；
[Speech 主入口](../../backend/src/speechnexus_backend/main.py)固定选择 Speech 工厂，
不会因 `process_role` 改成其他角色就启动 Workbench。
因此当前默认入口无法证明独立 Workbench 业务已接通，分机部署也无法只靠修改同一 API 地址完成。

处理：[实施 #226](https://github.com/hrygo/SpeechNexus/issues/226)补独立服务、上游配置与实际入口验证。
验收需证明路径和契约错误正确传递、关闭 Workbench 不影响 Speech；实时 owner 接线继续由 #24 负责。

### R2 · P1：共享运行依赖尚未按部署能力收敛

[Python 镜像](../../deploy/images/python.Dockerfile)安装所有 workspace 包，
[Backend](../../backend/pyproject.toml)把 `av` 列为基础依赖；
现有包边界不足以表达独立服务的最小安装环境、权限与原生能力。
这不证明 PyAV 只能运行于 Linux，也不证明共享镜像必须同机；实际风险是无关依赖和节点权限扩散，
以及缺少干净安装的可验证支持范围。

处理：[实施 #225](https://github.com/hrygo/SpeechNexus/issues/225)提供真实入口、依赖、配置与权限清单，
裁剪应用/执行能力制品。允许共用基础层，不要求一个角色一个包。
Scheduler/Job、实时和工作台业务缺口继续由 #22/#24/#42 实施，不重复建同义大包。

### R3 · P1：Linux 安全检查仍缺少跨部署边界的调用设计

[检查进程](../../backend/src/speechnexus_backend/speech/infrastructure/audio_inspection_process.py)
使用 Linux pidfd 等隔离/监督能力，现有接口为本地流/文件描述符；
[AssetRuntime](../../backend/src/speechnexus_backend/asset_runtime.py)仍默认使用 `TextAssetInspector`。
公开音频启用前，必须决定由哪个受控 Linux 组件提供检查，避免把应用的平台要求拉回 Linux。
现有文件描述符不能跨主机复用，简单改用普通 PID 又会破坏停止证据。

处理：[设计交接 #227](https://github.com/hrygo/SpeechNexus/issues/227)比较复用受控检查 worker 与独立服务，
选择最小形态，并固定对象、检查版本、限额、原身份、持久意图/回执和取消恢复。
本项关闭只证明设计与交接完成；#21/#30 仍须完成真实检查和公开音频资格。

### R4 · P1：Linux CI 与导入证据不能支撑原生跨平台声明

[当前 CI](../../.github/workflows/ci.yml)使用 Ubuntu runner；
[mypy 配置](../../pyproject.toml)覆盖全仓与 Linux 专属实现。
本会话已有 macOS 模块导入成功和 Linux 类型模式检查的局部结果，两者均不证明 macOS 的
干净安装、工厂启动、数据库/对象存储行为或媒体能力。不能仅在 macOS 上改用 Linux 类型模式消除报错。

处理：[实施 #228](https://github.com/hrygo/SpeechNexus/issues/228)按单元/能力建立两平台原生检查、
平台依赖隔离和必需汇总，保留 Linux 真沙箱与现有严格检查。
必要角色未装配时保持缺口；不靠不声明、默认关闭或跳过取得平台通过。

### R5 · P2：逻辑角色与网络服务需要区分

[Workbench runtime](../../backend/src/speechnexus_backend/workbench/runtime.py)内托管多个后台循环；
原规划分别列出角色，容易被读成必须为每个循环新增服务或立即拆包。
这会增加端点、部署与故障窗口，而现有持久 claim 能力、权限与扩缩需求才决定是否需要拆分。

本轮已在规划 §1.1、拓扑和 ADR 中明确：后台必须有归属、开关和多副本语义，可以声明托管；
CPU 媒体优先复用受控执行。API 与 Scheduler 经 PG/outbox 协作，不预设新的 API→Scheduler RPC。
图中拆出独立角色只表示可独立放置，不构成新增网络协议。

### R6 · P2：网关不能按无状态 HTTP 代理迁移

[入口所有权](../../execution-gateway/src/speechnexus_gateway/infrastructure/process_ownership.py)
使用 `fcntl.flock`，同一节点/网关的候选须共享节点私有锁目录；
[默认文件执行装配](../../execution-gateway/src/speechnexus_gateway/file_asr_runtime.py)核对
`host_node_id` 和 `gateway_id`。
[Controller](../../node-controller/src/speechnexus_node_controller/app.py)装配本地 daemon 与节点回执存储。
这些是当前适配器的真实放置约束，HTTP 可达性不足以证明可以跨主机迁走同一身份。

本轮已在规划与 ADR 中区分协议可移植、节点适配器和当前 Linux 资格。
保留入口所有权、generation 与停止证据；若未来迁移节点组件或替换锁实现，须单独设计等价隔离和恢复证据。
现有 #28/#29/#34 继续覆盖该范围，不另建重复“网关跨平台”任务。

## 3. 实施与门禁

| 跟踪项 | 交付归属 | 完成边界 |
| --- | --- | --- |
| [#225](https://github.com/hrygo/SpeechNexus/issues/225) | 平台制品；各组件提供入口 | 清单、按能力依赖、权限与干净装配 |
| [#226](https://github.com/hrygo/SpeechNexus/issues/226) | Web/Workbench 装配 | 独立上游、服务配置及真实请求验证 |
| [#227](https://github.com/hrygo/SpeechNexus/issues/227) | 资产检查；节点执行协作 | 最小部署形态与契约/消费者交接，不结算公开音频 ready |
| [#228](https://github.com/hrygo/SpeechNexus/issues/228) | 平台/CI；各组件提供检查 | macOS/Linux 原生检查、依赖隔离与必需汇总 |
| [D1 #229](https://github.com/hrygo/SpeechNexus/issues/229) | 组件维护者联合证据，用户放行 | 核验服务边界与两平台实际结果，不能由任务关闭自动放行 |
| [G5 #18](https://github.com/hrygo/SpeechNexus/issues/18) | 延续原联合发布职责 | 已增加 D1 和物理分机公开链/故障要求，保留原 G4、备用 GPU、HA 与全部发布 AC |

创建前检索 open/closed issue；已有 #21/#22/#24/#26/#28/#29/#34/#42/#43 的业务、
恢复与物理拓扑范围继续保留，新任务仅处理本次边界缺口，不改变原 25 主包分母。
总控 [#6](https://github.com/hrygo/SpeechNexus/issues/6)登记补充任务和 D1；未新建状态标签或修改分支保护。

**门禁执行顺序：** 制品、上游与媒体设计可以并行推进；CI 根据各单元制品落地，缺生产角色则等待其具名输入。
D1 提交同版入口/依赖/权限、两平台安装/严格检查/启动/持久行为和 Linux 专属验证；
G5 在既有前置满足后接收 D1，并核验物理主机分离的公开文件 ASR/TTS、已启用实时和跨机故障。
隔离 VM 可以提前联调，不能替代 G5 原有物理多主机/备用 GPU 验收。

## 4. 本次证据与限制

- 源码、配置和当前方案逐项核对；代码图谱当前无本项目索引，已用相关源码补证，未创建或重建索引。
- GitHub 已创建 4 个实施任务、1 个补充 Gate，更新总控和 G5 正文，并按最新正文回读核对；保留已有元数据和 AC。
- 本次只修改文档与 issue，未改产品实现、CI workflow、运行配置、数据库或引擎；同工作区其他 CI 与测试改动保留。
- 文档检查通过：14 份相关文档、345 条本地链接、7 份 frontmatter，以及代码围栏、issue 引用和空白；`git diff --check` 通过。
- 本轮文档验证不替代产品测试。macOS/Linux 原生矩阵、真实分机公开业务、网络分区与 HA 本轮均未执行，门禁保持未放行。

[部署规划](../architecture/deployment-units.md) · [部署入口](../../deploy/README.md) ·
[验证指南](../agent-verification.md)
