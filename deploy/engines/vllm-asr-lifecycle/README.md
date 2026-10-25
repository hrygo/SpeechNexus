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
