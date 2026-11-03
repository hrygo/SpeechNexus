---
title: "SpeechNexus 工程目录与依赖边界"
status: active
version: "0.4.1"
date: 2026-10-26
---

# SpeechNexus 工程目录与依赖边界

本文记录工程布局、依赖方向与扩展入口。产品语义仍以[主设计](../speechnexus-product-architecture.md)为准；当前组合与缺口见[交付跟踪矩阵](../plans/delivery-tracking.md)，[2026-10-26 项目经理基线](../plans/2026-10-26-project-manager-baseline.md)是历史快照。不能由目录存在或健康检查推断完整能力。

## 1. 工作区布局

```text
.
├── contracts/                   公共、管理与内部契约
│   ├── speech-openapi.yaml      OpenAI 子集与 nexus 语音 / Job / 音色
│   ├── workbench-openapi.yaml   可选工作台应用资源
│   ├── admin-openapi.yaml       管理面、实例、策略与审计
│   ├── realtime-events.schema.json
│   ├── scenario-profile.schema.json
│   ├── voice-resource.schema.json
│   ├── model-binding.schema.json
│   ├── model-instance.schema.json
│   ├── capacity-manifest.schema.json
│   ├── qualification-report.schema.json
│   └── internal/               执行控制、运行时控制、执行流与应用事务契约
├── migrations/                  历史协调 DDL 草案，不用于产品迁移
│   └── 0001_cluster_coordination_draft.sql
├── backend/                     FastAPI 语音底座
│   └── src/speechnexus_backend/
│       ├── app.py               ASGI 组合根
│       ├── settings.py          严格环境配置
│       ├── speech/
│       │   ├── domain/          领域策略、身份与状态机
│       │   ├── application/     资产、配置、协调、准入等用例与端口
│       │   ├── interfaces/      HTTP 投影与事务接口
│       │   └── infrastructure/  PG / 对象存储 / outbox adapter，含 Alembic 迁移
│       └── workbench/           可选工作台及独立 ASGI runtime
├── execution-gateway/           独立引擎执行网关
│   └── src/speechnexus_gateway/
├── node-controller/              受限节点控制器（批准模板的实例生命周期）
│   └── src/speechnexus_node_controller/
├── packages/
│   ├── contracts/              同源契约、校验器与生成消费者
│   └── media/                  原生媒体类型/decoder 与私有有界检查
├── web/
│   ├── admin/                   AdminUI Vite 应用
│   ├── workbench/               个人语音工作台 Vite 应用
│   └── shared/                  React 组件、设计 Token 与后续 SDK
├── deploy/                      Compose、镜像与环境变量模板
├── tests/
│   ├── contract/               契约与实现一致性
│   ├── fake/                   确定性端口替身
│   ├── integration/            隔离 PG / InnoDB 协调与对象存储测试
│   ├── fixtures/coordination/  旧 PG SQL 历史证据与事务契约样例
│   ├── support/                双库测试原型，不供产品导入
│   └── unit/                   领域、应用与 HTTP 单元测试
├── scripts/dev-env/            本机开发环境与验证脚本
└── docs/                       产品、架构、计划与验收记录
```

`backend/speech` 与 `backend/workbench` 是同一 Python 包中的明确模块边界；工作台已有独立 `workbench/runtime.py` ASGI 工厂及自有 PG adapter。模块分离不要求另建数据库，但 schema/对象区与写入所有权分开；工作台不向语音核心反向写入，也不让底座依赖工作台业务。

## 2. Python 工作区

根 `pyproject.toml` 是虚拟 `uv` workspace，成员为 `backend`、`execution-gateway`、`node-controller`、`packages/contracts` 与 `packages/media`。成员使用 `src/` 布局、独立包名与 `py.typed` 标记，已实现进程入口的成员声明独立入口点：

| 包 | 入口 | 当前职责 |
| --- | --- | --- |
| `speechnexus-backend` | `speechnexus-api` | Workspace、资产、配置和绑定管理，以及可配置的同步语音入口；私有词典草稿不代表公开创建，完整异步 Job 与实时链仍须验收 |
| `speechnexus-execution-gateway` | `speechnexus-gateway` | 持久提交/停止/未知隔离/执行观察、认证内部 HTTP、输出区间及文件 ASR/TTS 默认工厂；固定版本的同步 TTS 公开链及原租约释放有[受控窗口证据](../audits/2026-11-03-controlled-tts-prepare.md)，完整资格与其他部署仍须分别验收 |
| `speechnexus-node-controller` | `speechnexus-node-controller` | 受限节点控制器的意图、generation / fencing、drain 与停止证据规则，本地持久意图与容器 daemon adapter，`/healthz`、`/readyz` 与 `internal/runtime-control` 路由；独立镜像源码候选已有，实际部署待验 |
| `speechnexus-contracts` | 库 | 机器契约和生成 Python/TS 消费者，固定源码与生成清单共同识别预发布版本 |
| `speechnexus-media` | 库 / 私有检查命令 | 原生媒体类型、decoder 和私有有界检查；不授予公开 ready、Job、发布或停止权限 |

网关另提供 [CPU VAD 原生接口](cpu-vad.md)，实现会话隔离和有界切段；固定 ONNX CPU
adapter 的 vendor 依赖由 `deploy/engines/cpu-vad/` 独立锁定。文件 ASR 已有显式配置的默认工厂与隔离真实执行证据；Compose 配置了私有 ASR 文件路径，仍须提供相应部署配置并独立验证，公开实时链仍待接入。

依赖图由 `import-linter` 检查：

```text
interfaces ─┐
            ├──> application ──> domain
infrastructure ─┘

composition root ──> any layer
```

上层可以依赖下层，下层不得导入上层；interfaces 与 infrastructure 为独立同层，互不导入。speech 与 workbench、backend 与 gateway、node-controller 由 independence 门禁约束；工作台仅使用公开协议客户端，不能导入 speech 内部模块。`app.py` 是例外组合根；它装配接口、readiness 服务和基础设施 adapter，但不承载业务规则。

配置由 `pydantic-settings` 验证。缺少 PostgreSQL、对象存储探针或桶配置时进程启动失败，避免以未声明的本地路径或共享服务地址静默运行。

## 3. Web 工作区

根 `pnpm-workspace.yaml` 管理 `web/*` 和 `packages/contracts`，当前包括 Admin、Workbench、共享组件与契约包。共享 React 版本、Vite、TypeScript、测试库集中在 catalog，相关依赖通过 `catalog:` 引用。

- `web/admin` 与 `web/workbench` 各自构建、预览与部署，输出独立 `dist/`。
- `web/shared` 只包含跨入口稳定复用、无业务所有权的组件与视觉 Token。
- 工作台业务代码不得进入共享包，除非已确认它被两个入口以相同语义消费。
- 当前 UI 明确显示共享部署边界与未实现状态，不展示虚构任务、用户、容量或模型数据。

ESLint 使用 flat config 与 TypeScript 类型感知规则；TypeScript 开启严格空值检查、精确可选属性与未检查索引访问。格式统一由 Prettier 检查。

## 4. 契约生命周期

契约流程为：

```text
设计澄清 → 机器 schema → 正负样例 → fake 验证
  → 应用用例 → adapter → 契约测试 → 真实环境资格
```

`contracts/operations.openapi.yaml` 仍是运营健康检查的权威定义。P0 公共语音、实时事件、工作台、预设、音色、管理面和内部执行 / 实例 / 资格契约已交付；它们冻结协议形状，不表示业务路由已实现。完整清单与 OpenAI SDK 兼容门禁见[契约索引](../../contracts/README.md)、[OpenAI 兼容与增强边界](../architecture/openai-compatibility.md)和[整体技术方案](../architecture/technical-blueprint.md#3-契约与类型生成)。以下规则适用于每个新契约：

1. 先定义路径、字段、状态、默认值、单位与稳定错误码。
2. 为正例、未知字段、非法枚举和状态竞争提供样例。
3. 从契约生成或校验 TypeScript / Python 消费类型，禁止三处手写。
4. 将 speech、workbench 与 MCP 的等价入口映射到同一规范命令。
5. 未完成语义验证的文件不能因为 schema 语法通过而标记为已冻结。

## 5. 测试层级

| 目录 | 默认依赖 | 用途 |
| --- | --- | --- |
| `tests/unit` | 无外部服务 | 领域规则、应用用例、HTTP 响应与配置 |
| `tests/contract` | 无外部服务 | OpenAPI / Schema 结构与实现路径一致性 |
| `tests/fake` | 无外部服务 | 确定性端口替身与故障注入 |
| `tests/integration` | 隔离 PG / MySQL / MinIO | 受控事务、旧 PG 独立兜底、租约、outbox、对象发布与恢复 |

`make verify` 还执行媒体和浏览器基础冒烟检查：前者对合成音频验证重采样及 WAV / MP3 编解码，后者使用虚拟媒体设备检查录制、离线音频处理和回环 WebSocket。它们不证明真实麦克风、扬声器播放、模型质量、容量或完整业务流程。

## 6. 部署与凭据

服务拆分与平台要求以[部署单元规划](../architecture/deployment-units.md)和 [ADR 0011](../decisions/0011-distributed-deployment-and-platform-boundaries.md)为准。workspace、Python 包和当前共享镜像不是物理部署边界；Speech / Workbench API、后台角色、节点执行与数据服务可分机部署。当前开发脚本的 Linux ARM64 / systemd 要求只适用于该开发环境，各单元的原生平台与容器运行能力须分别验证。

`deploy/compose.yaml` 提供单节点开发拓扑。宿主端口绑定回环地址，凭据由 `deploy/compose.env` 提供且被 Git 忽略。应用服务配置只读根文件系统和 `no-new-privileges`；各服务的用户和运行限制以 Compose 配置及镜像为准，Web 镜像使用非特权 Nginx。

该拓扑不是生产交付。生产前仍需确定入口委托、Host / Origin 策略、TLS、对象存储凭据、数据库迁移、备份恢复、模型绑定、容量 manifest、监控与发布资格。

## 7. 按改动选择验证

检查命令与隔离前置统一见 [Agent 验证指南](../agent-verification.md)，以根 `Makefile`、`package.json` 与 CI 核验实际入口。文档仅检查链接、命令、声明及 diff；Python、Web、契约和持久化分别运行适用检查。依赖缺失或 lockfile 变化且任务需要时才锁定安装；相关改动、失败或未解疑点才扩大验证。

`make verify` 是包含环境检查与项目测试服务启动的完整本机入口，运行前须满足 Linux ARM64、用户级 systemd 等前置及隔离条件。协调事务或 DDL/权限变更保留 `make coordination-test` 与 `make integration-test` 双库门禁；缺服务、配置或宿主条件就报告未验证范围，不改向共享实例，也不以跳过或单库代替验收。

## 8. 后续工程交接

[整体技术方案](../architecture/technical-blueprint.md)确定显式 SQL / 有界连接池、版本化迁移、契约类型生成、内部 HTTP / WS 控制与数据流、受限实例控制器、Web 服务端缓存及部署内观测。它们是后续实施基线，不表示当前目录已有相应实现；新增工具应在对应工作包中锁定版本并验证现有 Python / Node 工具链兼容。

公共协议、内部协议、领域对象和 vendor DTO 分别维护边界转换；生成类型不进入 domain。迁移、事务 / 故障样例、fake controller 与资格包是可交接产物，不能以新建占位目录作为完成证据。工作台 LLM 仅使用本机 / 内网服务，业务内容不得出网，详见 [ADR 0003](../decisions/0003-workbench-llm-network-boundary.md)。

[架构文档](../architecture/README.md) · [项目主设计](../speechnexus-product-architecture.md) · [开发环境](../development-environment.md)
