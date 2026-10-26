# 固定 vLLM ASR 请求清理插件

这是运行在引擎内的独立 Python 3.12 wheel，保留 vLLM 原生 ASR serving、预处理、分段、语言检测、
采样与响应。主项目 Python 3.14 不导入 vLLM，也不将它加入共享依赖。

当前只完成**请求句柄归属与取消清理**。它不产生停止证明，不确认 GPU 资源释放，不归还预算，
不接通默认网关业务执行。TTS/Omni、实时流与目标多机资格另行实施。

## 固定运行时与启用

本机既有 ASR 镜像 ID 为
`sha256:61cc94fec25198aeefcd93b96088a8e8db106d61d834a36d3b89ba7bf5b10765`，
vLLM `0.28.0` / `g2cf0a6915`。`runtime-pin.json` 检查版本及依赖的关键源码摘要；摘要验证
不代替部署控制器对整个批准镜像的校验。上游升级需要重新核验并显式更新，不能放开版本检查。
该制品是本机 `local/qwen3-asr-vllm` 镜像，不声称此 ID 可从公共镜像仓库直接拉取。

从仓库根目录构建：

```bash
uv build --wheel --no-sources deploy/engines/vllm-asr-lifecycle --out-dir dist/python
```

在该固定镜像的**派生镜像构建阶段**离线安装 wheel，使用 `--no-deps --no-index`，不升级原镜像依赖。
显式将 `speechnexus_asr_scope` 加入 `VLLM_PLUGINS` allowlist，并关闭原生请求/输入日志。
仅安装 wheel 不会启用 endpoint plugin。通过批准的模板与原控制器部署；本目录不提供任意
Docker 管理入口，不改变现有服务或向公网暴露端口。

插件只为原 `/v1/audio/transcriptions` POST 加 ASGI 请求作用域，没有新增或覆盖路由。非 ASR
请求与其他原生 engine consumers 保持原行为。启用时要求原生 `AsyncLLM`、原 transcription
handler 和关闭的请求日志；不兼容时初始化失败。不要依赖未通过资格的部署配方自动降级。

## 清理顺序

1. 原生 ASGI 请求开始时取得一个有界作用域。它覆盖上传、语言检测、所有分段生成和响应发送；
   响应发送期间断开也会进入 `finally`，不依赖生成器垃圾回收来封闭作用域。
2. 原生 `add_request` 作为受保护的任务运行；在 `_add_request` 可能登记/外发前，记录实际
   internal ID、collector 与 parent ID。不从 HTTP ID 推测内部随机 ID，不重复派发。
3. 请求结束或取消后立即 seal，禁止新的子请求外发；取消仍在推进的消费者，但不取消已在途的
   native add。等整个 add（包括预处理与 n>1 fanout）结束，才按已登记 ID 清理。
4. 关闭原生生成器，清理原前端映射与 parent，然后经同一原生 EngineCore client 发送全部
   已登记的 internal ID。即使先前原生 abort 已删掉前端映射，也不能将空映射当作无需取消。
5. 分别尝试关闭所有 collector。失败或挂起保留本地占用；等待清理超时不能取消或遗忘 native add。

原生 serving 的取消处理还会调用 facade 的 `abort(external IDs)`。该入口在当前作用域内解析
其实际 external/internal/parent 关联，只能取消本作用域拥有的请求；先等自己的在途 add，
不等待或取消其他作用域。两个并发请求可以保留相同 `X-Request-Id`，不能通过禁止或改写它来
代替所有权隔离。重复取消和猜中的其他作用域内部 ID 也不能扩大范围；非作用域消费者仍沿用
原生行为。仅依赖生成器退出时的清理不足以覆盖这条 serving 取消路径。

活作用域上限 256；每作用域最多登记 4096 个内部请求，超限在额外外发前拒绝。本地清理容量
耗尽时沿用原 serving 构造私有 503 错误，不进入媒体解析/派发。上限只约束插件内存，不是 A 的
准入、槽位或 GPU 预算权威。成功清理删除本地句柄；重启后不由缺失的内存记录推断停止。

`wait_closed` 仅观测这些本地清理动作。core abort 消息发出、collector 关闭、SSE 结束、请求
数量为零或实例暂停都不能单独证明 GPU 停止与资源释放。

## 显式 worker 扩展与观察契约

部署模板可以另行将 `worker_extension_cls` 配置为
`speechnexus_vllm_asr.worker_fence.WorkerFenceExtension`。这是 vLLM 的正式 mixin 入口；
安装 wheel 或启用 endpoint plugin 都不会自动配置该扩展。它仅增加
`speechnexus_observe_worker_fence`，不覆盖 worker 原方法，不新增 HTTP 路由。
沿用镜像已经安装的 `jsonschema>=4.23,<5`；派生镜像仍离线 `--no-deps --no-index` 安装，
依赖不满足时构建/验证失败，不在线升级固定运行时。

源机器契约是 wheel 内 `worker-fence.schema.json`，请求和响应直接使用它校验。
请求采用 `worker-fence.v1`、32 位小写十六进制 nonce 和 1–4096 个不重复的实际 internal ID；
单 ID 上限 1024 字符，整个紧凑 JSON 上限 524288 UTF-8 字节。调用方每次生成新 nonce。
worker 返回相同 nonce、排序 ID 集合摘要及实际 rank，不回传 ID、模型内容或路径。
`validate_response` 校验协议、结构、nonce 和摘要；未来的组合消费者还必须核对全部预期 rank、
实例/generation 与原核心执行，不能只接受某一个 worker 的响应。

该扩展首次使用时验证独立 `worker-pin.json` 的固定版本与 21 个源码摘要。原生 GPUModelRunner
之外的布局返回 unsupported；未知状态、源码差异和同步异常不能产生成功观察。
它只统计原 IDs 在 worker request state、persistent batch、prompt logprob state 中的项数。
仍有这些项、未结束模型步骤或 PP send handle 时返回 pending，不修改它们。
否则对实际 worker device 同步 GPU，再重读状态；只有两次检查都允许完成才返回 observed。
此同步可能等待同一 worker 上的其他在途 GPU 工作，不取消其他请求，也不暂停整个实例。
有界等待由未来的引擎内调用方管理；超时不能取消 native RPC 或推断同步完成。

**任何状态的 `release_evidence` 都是 false，包括 observed。** 它没有核实原核心接收、提交
顺序、尚未消费的批次/输出、调度器 KV/encoder 资源、connector 或部署代次；未知 ID 也可能
仅表现为 worker 没有可见状态。此响应不能转换成网关停止证据、资源释放证明或直接退预算。
不要求共享 encoder cache、模型权重和 CUDA allocator 回到空闲前的显存数值。
async、TP/PP/DP 和 connector 的完整组合资格仍由后续生产者联合取证，不能据本观察自动放行。
上游、布局或观察语义变化要重验源码 pin；改变字段或含义使用新协议版本，旧响应不兼容时拒绝。

确定性正负例、fake worker 与响应绑定回归：

```bash
uv run pytest tests/unit/gateway/test_worker_fence.py tests/unit/gateway/test_native_asr_scope.py -q
```

## 验证与后续

确定性测试覆盖可能已接受但回执未返的 add、回执异常、分段/语言检测、部分 n>1 fanout、
独立请求隔离、旧前端映射消失、清理异常、有界拒绝及流式 ASGI send 断开：

```bash
uv run pytest tests/unit/gateway/test_native_asr_scope.py -q
```

`probe_native.py` 在固定镜像中消费实际安装的 wheel、正式插件加载器、原生 `AsyncLLM`
generate/add、`ParentRequest`、前端 abort 和 collector close；其 IPC、输入与前端状态为受控
替身。它不加载模型、不使用 GPU、不证明真实传输或停止资格。执行环境应为无网络、无 GPU、
只读根文件系统及有界 CPU/内存的本项目临时容器；使用镜像的 `python3`。

下一阶段要在原请求封闭与所有在途 add 完成之后，验证同代次的核心执行屏障、所有相关批次及
延迟释放/connector 事实，再冻结并接通受限的引擎事实协议与网关 observation/abort/停止证据消费者。
随后完成真实模型完成/取消/崩溃探针、有效参数和最终制品发布。不通过整套 dev API、仅 HTTP
状态或暂停数量推断停止。集群支持保持原要求；当前单机证据不能替代物理多机资格。

来源：固定镜像内 vLLM Apache-2.0 实现。项目没有复制 vendor 源码；本 overlay 调用其既有
方法。关键路径与摘要记录在 wheel 中的 `runtime-pin.json`，源码核验原件仅保存在本机 `.runtime/`。
