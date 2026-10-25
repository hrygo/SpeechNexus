---
title: "受控文件 ASR 的原生鉴权与代次关联"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 受控文件 ASR 的原生鉴权与代次关联

本单元供 B01/B02/B03 的首条文件 ASR 接线使用。控制器只对获准模板注入原生
`VLLM_API_KEY`，网关消费部署时固定的目标和派生凭据。复用现有容器、文件执行、
原 intent、结果回执与预算协议，不增加身份服务、协调服务、事实表或业务路由。

## 凭据的作用域与所有权

控制器和部署工具持有 32 字节随机根密钥，编码为 64 位小写十六进制，通过
`SPEECHNEXUS_CONTROLLER_ENGINE_AUTH_ROOT_KEY` 的 secret 配置提供。生产环境不得使用
测试的全零根密钥。根密钥不进入模板、命令、运行回执、公开响应、日志或网关配置。

唯一派生实现在
[EngineCredentials](../../node-controller/src/speechnexus_node_controller/infrastructure/engine_credentials.py)。
其 HMAC-SHA256 输入为以下有序 JSON 数组，以 ASCII、无额外空白编码：

```text
["speechnexus.vllm-api-key.v1", host_node_id, gateway_id, pool_id,
 instance_id, instance_generation, binding_revision_id]
```

HMAC key 是根密钥的原始 32 字节，输出为 64 位小写十六进制。改变任一目标维度都会改变
凭据；同一代次在控制器重启后保持相同。`target_node_id` 映射为命令侧 `host_node_id`。
原请求的 manifest、配置摘要仍由现有实例/准入契约冻结、核对，不能在同一代次偷偷更换。

本凭据不随 intent、租约或 fencing token 改变，不承担准入和任期裁决。旧任期在同一代次
中已外发的请求仍须由现有提交屏障、入口封锁和停止流程处理。根密钥轮换不会恢复旧容器：
不匹配的容器隔离，按原身份停止后使用新 generation、重新预热和准入。

## 生产者：受限控制器

获准模板显式设置 `engine_auth: "vllm_api_key_v1"`；省略时维持既有非鉴权模板行为。
此模式要求明确 argv，以及 `/v1/audio/transcriptions` 的 `http_warmup_ok`。拒绝
`--api-key`、相关缩写/下划线拼法、配置文件、root-path 和 middleware 覆盖；
固定镜像中 CLI api-key 会优先于 `VLLM_API_KEY`，不能允许模板静默换掉凭据。

控制器通过 Docker `Env` 注入派生值，不把它放入 argv 或 labels。新建后和恢复观察时，
核对实际镜像 ID、argv、唯一原生 key、节点/网关/池/实例/代次/binding/模板/配置/manifest
labels，并保持原内存和网络限制检查。缺密钥的配置在启动前失败；漂移实例保持 quarantine，
不预热、不标 ready、不签发停止事实，但原身份的安全停止路径仍可使用。

预热使用同一 Bearer，继承累计期限、固定请求/响应上界、不代理、不重定向及不向第二 IP
重试 POST 的规则。缺失/错误凭据或旧代次预热不能获得 ready。`/health` 是未鉴权入口，
其 200 不能替代受保护路径预热。

模板审批还必须确认固定镜像的 entrypoint、原生鉴权行为和网络暴露范围。此模式不是任意
引擎的通用认证插件，不能仅改模板字段就宣布一个未验证镜像支持它。

## 消费者：网关文件命令

[私有部署描述机器契约](../../deploy/engines/vllm-asr-lifecycle/controlled-asr-endpoint.schema.json)
定义 `protocol`、`target`、固定私网 IP/port、served-model 和派生 key。描述文件必须为
普通私有文件、无 group/other 权限、不是符号链接，最多 16 KiB；加载器拒绝重复或未知字段。
JSON Schema 定义形状；部署网段限制、文件权限和原生凭据是额外执行语义。

部署输入与输出均留在忽略的私有运行目录。输入是上述描述删去 `bearer_token`：

```bash
uv run python scripts/dev-env/provision-controlled-asr.py \
  --input .runtime/config/asr-target.json \
  --output .runtime/config/asr-endpoint.json
```

此命令仅离线生成 mode 0600 文件，不覆盖现有文件、不访问服务、不注册 binding 或发布资格，
不输出密钥。部署者应使用控制器相同根密钥和 A 已批准的目标，分别向网关分发相应描述文件；
网关只拿该代次派生值，不能挂载根密钥。配置不得来自业务请求或可变客户端端点。

组合根调用 `load_controlled_asr_endpoint` 后构建 `VllmAsrRecognizer`，再交给现有
`CommandFileAsr`。命令在对象读取前，按原持久身份核对全部六个目标维度，处理中继续检查；
不一致时沿现有错误边界拒绝，不读取音频、不外发、不新建意图。未绑定目标的老式原生
endpoint 仅支持独立诊断，不能通过文件命令执行。真实默认工厂、A 配置选择和公开发布链
尚待装配，不能把可加载的描述或本单元的 fake 组合当作已完成接线。

## 保证边界与兼容

这是受信控制器和部署网络内的代次隔离：新受控代次使用不同原生 key，复用 IP 时拒绝旧请求。
它不提供 TLS 服务端身份认证，不能抵抗恶意替换的服务、被攻陷的容器守护进程或在允许
地址上运行的未鉴权服务。仍须按部署设计限制引擎网络入口，禁止业务 API/worker/工作台
直接访问引擎；私网 IP 或 API key 本身不证明出站受限。

既有公共协议、提交身份、预算和停止协议不变。新字段对旧模板默认关闭；启用新模式需先升级
控制器并安装根密钥，生成匹配的网关描述，再以新 generation 预热。旧控制器拒绝未知模板
字段，不能在滚动升级中混发。原生 endpoint 的独立诊断调用维持兼容，但持久文件消费者
现在拒绝未绑定代次的真实 endpoint；不得通过去掉目标校验恢复接收。

错误 Bearer 的请求虽然可在原生 ASGI 中间件读取正文前被拒绝，HTTP 客户端仍可能已向
目标发送正文，不能据此宣称内容从未进入错误端点。取消、401、EOF、超时或响应成功也不能
签发停止/释放；未知继续占用，只有 A 原事务凭匹配的双证据归还预算。

## 证据与未验收项

确定性验证覆盖派生向量、六维作用域、真实回环预热、Docker JSON 序列化/检查、漂移隔离、
旧代次拒绝、文件命令 I/O 前校验、配置文件安全和实际消费者读取。它们不加载模型。

`probe_native_auth.py` 在
[固定镜像](../../deploy/engines/vllm-asr-lifecycle/src/speechnexus_vllm_asr/runtime-pin.json)
的无 GPU、无网络、只读、限资源容器中导入实际 vLLM 中间件，并核对
[源码 pin](../../deploy/engines/vllm-asr-lifecycle/native-auth-pin.json)。
2026-10-25 的四个组件场景通过：缺 key/旧 key 为 401 且正文读取零次，新 key 进入处理器，
未鉴权 `/health` 可达。不加载模型，不签发资格；原始输出留在忽略的本机证据目录。
可将该引擎目录只读挂载到 `/probe`，在准确镜像中执行
`python3 /probe/probe_native_auth.py` 复核；镜像身份在宿主确认。

真实默认文件工厂、受控模型实例、网络隔离、持久执行/停止/预算及公开 SDK/C 下载仍分别
待联验。单机多逻辑消费者与物理集群资格继续分列，B00–B07 和 #142 不因本单元通过而完成。
