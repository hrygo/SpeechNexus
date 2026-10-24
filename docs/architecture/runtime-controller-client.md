# Runtime 控制器 HTTPS 客户端

属于 [A00 #19](https://github.com/hrygo/SpeechNexus/issues/19) 和
[A03 #22](https://github.com/hrygo/SpeechNexus/issues/22)。
`application.runtime_controller.RuntimeController` 是异步窄端口，
`infrastructure.runtime_controller_http.HttpRuntimeController` 实现对一个固定节点的
submit/query。输入是 A 已提交并保存的完整原 `RuntimeIntent`，输出是闭合、匹配的
`ControllerObservation` 及有界原观察正文。客户端不创建资格凭据、不修改数据库。

**持久 outbox 派发、owner 接管协调、节点内观察循环、批准/真实资格 provider、运行进程
与部署 TLS 配置仍待接入。** 不能因客户端存在而开放业务入口或宣称实际调度闭环完成。

## 契约与校验

原源为 `contracts/internal/runtime-control.openapi.yaml` 0.1.0.dev0，未新增 wire 字段。
A 的同一生成流水线新增 `RuntimeControlErrorResponse` runtime 入口和 runtime HTTP surface：

| 操作 | 路径 | 入口 |
| --- | --- | --- |
| `submitRuntimeInstanceIntent` | `POST /internal/v1/nodes/{node_id}/instance-intents` | 完整原请求和声明响应 |
| `getRuntimeInstanceIntent` | `GET /internal/v1/nodes/{node_id}/instance-intents/{intent_id}` | 原身份查询和声明响应 |

Python 使用 `speechnexus_contracts.runtime_http` 的 `validate_request` /
`validate_response`；TypeScript 使用 `@speechnexus/contracts/runtime-http`。
两者从同源解析 path/header/status/error，使用
`tests/fixtures/contracts/runtime-http.json` 的39个正负样例，不填默认值或改写输入。
TS 的生成客户端提供传输校验；固定网络、凭据和调用预算由具体运行客户端负责，
不能把离线 schema 检查当作网络资格。

Python adapter 使用已有 `runtime_codec.prepare_request` 与
`runtime_observation_codec.prepare_observation`。固定的节点、intent、pool、gateway、模板、
binding/manifest/config、generation、action、fencing 和预留内存全部匹配后才返回观察。
GET 不接受 POST 专用 replay 标志。观察协议不回显 deadline，原命令一致性仍依赖
B 的不可变 node/intent 请求和精确重放约束；查询不授权更换 deadline 或新建身份。

## 固定网络与调用预算

`RuntimeControllerEndpoint` 只接受部署配置的单节点 ID、私有/loopback IP 字面量、端口和
每节点组件 bearer token；不接受客户端 URL、DNS 名称、link-local/metadata、全局、
未指定、多播、IPv6 zone 或映射地址。凭据与地址不进入 endpoint repr 或错误信息。

协议固定 HTTPS，证书链及 IP SAN 必须通过，最低 TLS1.2；提供的 SSL context 不允许关闭
证书/主机名验证。每次调用重核对 context，不能在建 client 后修改它来禁用验证。
HTTP transport `trust_env=False`、不跟随重定向、不自动重试，不消费 netrc 或代理设置。
默认控制器 HTTP 监听须由部署提供批准的内网 TLS 终结；本单元未修改部署或接入 MegaCube。

`timeout_seconds` 必须显式设置，范围 `(0, 1800]`；总期限包含等待槽位、建立连接、响应头、
全部响应体及收尾。逐块慢流不能无限续期。POST 的预算还受原 intent deadline 限制，
过期或在队列中到期不再发送；这不是服务器已停止的证据。GET 允许查询过期的原意图。
并发1–16、等待队列1–64，默认4/16；队满立即失败，取消/超时释放本地槽位。

请求使用已冻结的完整正文，有界65536字节。响应预算可配置至多65536字节；要求 JSON /
UTF-8，拒绝压缩、重复键、非有限数、重复/不匹配的相关 ID、异常长度、未声明状态和不匹配
的观察。流异常及时关闭连接；错误只包含受控分类/原契约错误码，不复制服务端 message。
`_transport` 仅作合成单元测试注入，真实 TLS 证据来自默认网络 transport。

## 结果未知与预算

一次调用只发一个 HTTP 请求。`ControllerCallError` 是调用未取得可用事实，不是物理停止、
执行失败、未外发证明、派发许可或预算释放证明。包括401、409、503和原身份404，都不修改
A 的预算。`request_may_have_run=False` 只说明本次调用尚未发出，不能推断早先的调用未执行。
断连/超时后查询原 node/intent；不自动重发 POST，不换 intent/generation。
外层 `CancelledError` 原样传播，HTTP 取消不能停止容器或返还 peak/steady。

控制器一次 prepare/inspect 可持续一个预热期限，调用方可能先超时而节点动作仍继续。
已有[运行时事务](production-runtime-intents.md)只有在受信 readiness 及数据库前提完整时，
才允许 peak→steady；只有停止、资源释放和活动租约/提交清理证据完整时才归还基线。
客户端返回 ready 或构造 `VerifiedRuntimeReadiness` 本身不证明真实资格。

## 节点观察生产者的依赖

当前 B 的 `get_intent` 只读已存事实，同键 `submit_intent` 也只返回精确重放。
二者都不调用 daemon.inspect；实际 inspect 在节点内 `reconcile_instance`。
现有 `reconcile` HTTP 接受外部快照，A 不能伪造节点事实来推进预热。

因此 GET 可能持续返回 loading/warming_up，直到节点本地生产者完成观察并持久化。
实际控制循环还需 B 交付节点内观察生产者，或双方冻结后续窄协议；还需部署的节点 TLS
地址/CA/组件凭据和 A 的批准/真实资格 provider。未具备时保持预算，不能靠客户端重试制造
新事实或 fake ready。这个依赖不阻止 A 的其他独立工作包继续实施。

## 验证边界

单元测试验证总期限、慢流、队列、取消、关联、身份、证书策略、畸形响应和不重试。
`test_runtime_controller_client.py` 在真实 loopback TLS、原生 B controller/SQLite 与隔离
PG/InnoDB 上验证 A create→HTTP→A observe、原应用重启/精确重放、证书链/IP SAN 拒绝、
真实未答复 POST 后查询原意图、认证/404期间预算保持、warming_up保留peak，以及停止后返还。

测试显式调用 B 节点内 reconcile 产生合成观察，再用只读 GET 消费；这不证明节点观察循环
已经运行。daemon、批准计划与 readiness 均为合成事实，内存按合法 MiB 单位换算以满足 B
真实的6 MiB模板约束；不加载模型、不启动容器，不证明 GPU、预热质量、内容网络或多主机资格。
