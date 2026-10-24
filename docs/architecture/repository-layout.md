---
title: "SpeechNexus 工程目录与依赖边界"
status: active
version: "0.3.8"
date: 2026-10-24
---

# SpeechNexus 工程目录与依赖边界

本文记录仓库初始化后的实际工程布局、依赖方向与扩展入口。产品语义仍以[主设计](../speechnexus-product-architecture.md)为准；P0 的公共语音、实时事件、工作台、预设、音色、管理面和内部绑定 / 实例 / 资格机器契约已交付，但对应业务路由与运行时行为仍待实现。

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
├── migrations/                  部署角色执行的版本化 DDL 草案
│   └── 0001_cluster_coordination_draft.sql
├── backend/                     FastAPI 语音底座
│   └── src/speechnexus_backend/
│       ├── app.py               ASGI 组合根
│       ├── settings.py          严格环境配置
│       ├── speech/
│       │   ├── domain/          领域策略与状态机（待 P0）
│       │   ├── application/     用例与端口（当前仅运营 readiness）
│       │   ├── interfaces/      REST / WS / MCP 投影
│       │   └── infrastructure/  PG / 对象存储 / outbox adapter
│       └── workbench/           可选工作台应用模块
├── execution-gateway/           独立引擎执行网关
│   └── src/speechnexus_gateway/
├── node-controller/              受限节点控制器（批准模板的实例生命周期）
│   └── src/speechnexus_node_controller/
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

`backend/speech` 与 `backend/workbench` 是同一后端部署中的明确模块边界。分离模块不表示必须拆成两个数据库或两个进程；首期只要求工作台不向语音核心反向写入，也不让底座依赖工作台业务。

## 2. Python 工作区

根 `pyproject.toml` 是虚拟 `uv` workspace，成员为 `backend`、`execution-gateway`、`node-controller` 与 `packages/contracts`。成员使用 `src/` 布局、独立包名与 `py.typed` 标记，已实现进程入口的成员声明独立入口点：

| 包 | 入口 | 当前职责 |
| --- | --- | --- |
| `speechnexus-backend` | `speechnexus-api` | 语音底座 HTTP 外壳；`/healthz`、`/readyz` |
| `speechnexus-execution-gateway` | `speechnexus-gateway` | 持久提交/停止/未知隔离/执行观察、受信正文清理与输出区间事务、控制库就绪检查；显式配置后提供认证 HTTP 屏障、只读执行查询与宿主进程独占；真实计量采集/消费、删除许可、引擎和 submit/abort 路由待实现 |
| `speechnexus-node-controller` | `speechnexus-node-controller` | 受限节点控制器的意图、generation / fencing、drain 与停止证据规则，本地持久意图与容器 daemon adapter，`/healthz`、`/readyz` 与 `internal/runtime-control` 路由；部署单元待集成 |

网关另提供 [CPU VAD 原生接口](cpu-vad.md)，实现会话隔离和有界切段；固定 ONNX CPU
adapter 的 vendor 依赖由 `deploy/engines/cpu-vad/` 独立锁定。默认启动不加载模型，公开实时链路
与实际 ASR 外发尚未接入。

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

根 `pnpm-workspace.yaml` 管理 `web/admin`、`web/workbench` 和 `web/shared`。共享 React 版本、Vite、TypeScript、测试库集中在 catalog，成员只引用 `catalog:`。

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

`contracts/operations.openapi.yaml` 仍是运营健康检查的权威定义。P0 公共语音、实时事件、工作台、预设、音色、管理面和内部执行 / 实例 / 资格契约已交付；它们冻结协议形状，不表示业务路由已实现。完整清单与 OpenAI SDK 兼容门禁见[契约索引](../../contracts/README.md)、[OpenAI 兼容与增强边界](openai-compatibility.md)和[整体技术方案](technical-blueprint.md#3-契约与类型生成)。以下规则适用于每个新契约：

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

`make verify` 还执行媒体和浏览器基础冒烟检查。它们证明本机工具链可采集与播放，不证明真实麦克风、模型质量、容量或完整业务流程。

## 6. 部署与凭据

`deploy/compose.yaml` 提供单节点开发拓扑。所有宿主端口只绑定回环地址，凭据由 `deploy/compose.env` 提供且被 Git 忽略。容器使用非 root Python 用户、只读根文件系统和 `no-new-privileges`；Web 镜像使用非特权 Nginx。

该拓扑不是生产交付。生产前仍需确定入口委托、Host / Origin 策略、TLS、对象存储凭据、数据库迁移、备份恢复、模型绑定、容量 manifest、监控与发布资格。

## 7. 每次实现的最小门禁

```bash
uv sync --locked --all-packages --all-groups
pnpm install --frozen-lockfile
make verify
```

只改文档时至少运行链接和格式相关检查；只改 Web 时至少运行 `pnpm format:check && pnpm lint && pnpm typecheck && pnpm test && pnpm build`；只改 Python 时至少运行 Ruff、mypy、import-linter 与 pytest。涉及持久化或调度时增加隔离 PostgreSQL / MinIO 集成测试；当前协调 DDL 的双库行为门禁为 `make integration-test` 与 `make coordination-test`，必须同时设置指向本项目隔离测试库的 `SPEECHNEXUS_TEST_DATABASE_URL` 和 `SPEECHNEXUS_TEST_MYSQL_URL`，缺任一变量直接失败，且只配置一个时共享事务用例按失败处理。

## 8. 后续工程交接

[整体技术方案](technical-blueprint.md)确定显式 SQL / 有界连接池、版本化迁移、契约类型生成、内部 HTTP / WS 控制与数据流、受限实例控制器、Web 服务端缓存及部署内观测。它们是后续实施基线，不表示当前目录已有相应实现；新增工具应在对应工作包中锁定版本并验证现有 Python / Node 工具链兼容。

公共协议、内部协议、领域对象和 vendor DTO 分别维护边界转换；生成类型不进入 domain。迁移、事务 / 故障样例、fake controller 与资格包是可交接产物，不能以新建占位目录作为完成证据。工作台 LLM 仅使用本机 / 内网服务，业务内容不得出网，详见 [ADR 0003](../decisions/0003-workbench-llm-network-boundary.md)。

[架构文档](README.md) · [项目主设计](../speechnexus-product-architecture.md) · [开发环境](../development-environment.md)
