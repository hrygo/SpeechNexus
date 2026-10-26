# SpeechNexus 部署集群与模型实例拓扑

两张互补的 **3840 × 2160** 终态目标图，按 2026-10-27 核对的有效设计绘制。
图面表达部署关系和运行约束；实际装配、平台资格和验收证据仍由[部署单元规划](../../deployment-units.md)维护。

| 图 | 阅读重点 | 交付文件 |
| --- | --- | --- |
| 部署集群总览 | 独立服务副本组、各主机的执行权限与预算、数据库和对象存储的不同集群形态 | [4K PNG](01-cluster-overview-4k.png) · [SVG](01-cluster-overview-4k.svg) · [预览](01-cluster-overview-4k.preview.png) |
| 模型、引擎与实例拓扑 | 全部模型与运行时、跨主机同绑定副本、逻辑池和跨模型依赖 | [4K PNG](02-model-instance-topology-4k.png) · [SVG](02-model-instance-topology-4k.svg) · [预览](02-model-instance-topology-4k.preview.png) |

## 构图依据与修正

原图用单个组件框加数量标记表示扩容，容易混淆四种实体：固定模型绑定、运行中的引擎实例、逻辑资源池和真实计算主机。
本版将集群全貌与模型部署拆成两个尺度；总览保留代表实例，细节图展开完整模型组合，避免把所有事实压入一张图。

| 层次 | 本图表达 | 对部署的含义 |
| --- | --- | --- |
| 模型制品 | A–H 八类模型及完整模型 ID | CustomVoice、Base、VoiceDesign 是不同权重；对齐、分人、VAD 各有运行时 |
| 固定绑定 | 模型版本、引擎与固定配置的修订 | A1/A2 等同色副本使用相同绑定修订；真实标识以机器契约为准 |
| 引擎实例 | A1、A2、C1、C2、D1、D2 等独立运行副本 | `instance_id` 和 `generation` 各自独立，每个受管引擎实例只有一个 `host_node_id` 和 `pool_id` |
| 逻辑资源池 | `asr-realtime`、`tts-realtime` 等跨节点集合 | 池聚合兼容实例容量；同模型可按用途部署到不同池，单个实例保持唯一归属 |
| 计算主机 | Host-A、Host-B、Host-C 及各自 Gateway/Controller | 同机所有池和实例共用一个父物理预算；不同主机独立核算 |

两图的 Host-A/Host-B 对应同一部署示例，总览省略部分实例，第二张补全。
Host-C 同时列出多种模型表示可共置关系，实际加载组合由固定资源版本、完整配方和父预算决定。
示例数量与位置不构成显存容量或吞吐承诺。
同模型扩容表示新增独立服务副本；单个实例的 tensor/pipeline parallel 多 GPU 切分需要另外的部署配置和资格证据。

## 各角色的集群规则

| 角色 | 扩容方式与边界 |
| --- | --- |
| 入口代理、Speech API、Workbench API | 增加同角色副本并配置独立上游；公开业务保持持久幂等与配额；工作台经公开语音协议消费底座 |
| Realtime Gateway | 多副本承接不同会话；会话固定 `owner_gateway_id` 和 `stream_epoch`，故障后的新 epoch 与缺口按实时协议处理 |
| Scheduler | 多进程分别持有池租约；每池唯一有效 owner，以 PG 租约和 fencing 约束接管 |
| Job Workers | 并发认领持久步骤，核验 attempt 与 fencing；outbox 可重放，业务写入保持幂等 |
| 工作台后台 | 可增加本模块 worker；只认领本模块持久工作，使用独立表所有权和权限 |
| Execution Gateway / Node Controller | 随各真实计算主机部署；持有本节点的执行入口和获准模板管理权限，保留节点身份与 generation |
| 同模型引擎副本 | 新增实例并完成绑定、容量声明和就绪资格；准入选定目标实例后提交，实时流保持 owner |
| CPU VAD / 媒体检查 | 按 CPU、会话状态和隔离能力扩容；图中 H1/H2 是 CPU 运行副本，媒体检查是逻辑执行边界，具体装配由调用与恢复契约决定 |
| PostgreSQL | 单写权威与备副本；写入一致性、复制与接管按选定数据库部署方案验证 |
| S3 兼容对象存储 | 统一端点后可有多个存储节点，固定版本对象的持久性和高可用由存储服务方案提供 |

资源池示例：A1/A2 属于 `asr-realtime`，C1/C2/D1/D2 属于 `tts-realtime`；
B1、E1、F1/G1 分别属于 `asr-batch`、`voice-creation`、`auxiliary`。
这是一组可评审的放置示例，部署可按目标能力调整；完整配方跨池时仍在同一 PG 事务中核算全部阶段资源。
实例基础占用、在途和停止未知的占用共同约束父预算；释放依赖可验证的停止与释放证据。

## 模型与引擎的关系

完整模型 ID、运行时与加载策略见图二及[模型清单](model-catalog.json)。

- A/B 分别为 0.6B 实时 ASR 与 1.7B 文件/完整段复核，运行于 vLLM；启用完整段复核的实时配方同时准备 B 的容量。
- C/D/E 分别为 CustomVoice、Base、VoiceDesign，使用各自的 vLLM-Omni 实例；VoiceDesign 候选经 Base 校验后发布不可变 VoiceRevision，再由 Base 复用合成。
- F 使用官方 `qwen_asr` 与 PyTorch CUDA，G 使用 NeMo 与 PyTorch CUDA；二者作为独立辅助实例参与配方。
- H 使用 CPU ONNX Runtime；同一运行进程可复用只读模型，会话状态独立。跨主机运行的是独立模型副本，VAD 的 CPU 运行按所在运行时验证。

总览把 VAD 与媒体沙箱放在 Linux CPU 节点示例中，以复用受控执行资源。
Linux 要求来自现有媒体沙箱；CPU VAD 本身按所在运行时验证。
工作台的内网 LLM 是可选的独立上游，SpeechNexus 的模型实例治理范围聚焦语音底座。

## 阅读约定与精确范围

- 绿色实线：入口、公开语音协议、内部执行或对象通道；灰色线：PG 事务状态；灰色虚线：实例控制；琥珀色线：CPU 执行能力。
- 模型同色点线：同绑定模型的独立副本关联。点线两端没有箭头，表达副本关系；业务请求由准入与 Gateway 路由。
- 服务分区包含多个可独立部署的进程与主机；数据区使用独立数据库和对象存储服务。
- 跨主机内容经固定版本对象引用与约定流协议传输，本地临时区私有且有界。跨机 TLS、浏览器资源和观测均遵守部署网络边界。

图中的 PG 备副本、S3 多节点与服务副本表达目标部署能力，具体高可用产品、切换策略及拓扑测试以各单元的有效部署配置为准。
总览连线按角色集合汇总逻辑通道，精确权限、调用、取消和恢复边界继续采用源文档与机器契约。

## 来源

- [部署单元与平台规划](../../deployment-units.md)
- [ADR 0002：模型、引擎与加载](../../../decisions/0002-speech-models-engines-and-loading.md)
- [ADR 0004：集群实体与角色](../../../decisions/0004-cluster-ready-architecture-and-single-node-mvp.md)
- [运行时资源治理](../../runtime-governance.md)、[技术蓝图](../../technical-blueprint.md)
- [模型绑定](../../../../contracts/model-binding.schema.json)、[模型实例](../../../../contracts/model-instance.schema.json)、[容量声明](../../../../contracts/capacity-manifest.schema.json)

## 混合制作与校验

复用前版经内置 `imagegen` 生成的三组透明插画，并重新绘制全部拓扑、矢量图标和文字。
插画只承担视觉识别，部署事实由可编辑 SVG 图层定义。[提示词](image-prompts.json)与 [assets](assets/)保留素材来源。
SVG 内嵌素材，可独立编辑；PNG 固定字体外观。两张 PNG 均直接以 4K 渲染，生成插画采用缩小放置。

本目录的独立构图工具使用 Python 标准库以及 Node.js、Playwright、Sharp，不依赖业务服务、模型或数据库。
在提供这些工具的环境中可重新生成：

```bash
python3 build_diagrams.py
node render_and_validate.cjs --channel chrome
```

使用 Playwright 自带 Chromium 时省略 `--channel chrome`。字体为 `Arial`、`PingFang SC` 或 `Hiragino Sans GB`；
其他环境重绘时需提供这些字体或重新核对替代字体的换行和间距。

检查覆盖原生像素尺寸、全部文字与模型标识、逐容器内边距、文字与图标间距、连线避让文字和插画、资源加载，以及图面过程性内容排除。
机械检查后打开两张 PNG 人工核对分区、模型归属、连线路径与阅读密度。
结果见[总览检查](01-cluster-overview-4k.check.json)、[模型检查](02-model-instance-topology-4k.check.json)与[综合验证](validation.json)。
