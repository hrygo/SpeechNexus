---
title: "B01/B03 原生 ASR 请求清理交付边界"
date: 2026-10-24
---

# B01/B03 原生 ASR 请求清理交付边界

对应 #28 / #30，[实施计划](../plans/2026-10-24-b03-native-asr-scope.md)与
[独立 overlay](../../deploy/engines/vllm-asr-lifecycle/README.md)。
最初基线 `fcb0670`，实施期间 ff-only 同步 A #131 至 `fd60556`，既有制品完整保留。

## 问题与实际变化

固定 vLLM 0.28.0 / g2cf0a6915 的 `generate` 在 `add_request` 返回前，尚未取得 collector。
受控源码方法探针显示：若登记/可能外发后取消，q 仍为 None，原生生成器不会执行该请求的
abort/close。原生 abort 又会先移除前端映射，再发送消息；后续再查空映射不能证明核心已取消。
这两项是源码分支证据，不宣称已经实测发生真实 ZMQ 竞态。

overlay 通过正式插件入口，在原生 ASR ASGI 请求中登记实际 internal/child/parent ID，
保护整个在途 add；请求封闭后禁止追加子请求，等待 add 结束，再清理前端、按已登记 internal ID
经原生 core client 发取消并关闭 collector。响应发送时断开也进入清理，不依赖仅包装某个
生成器的垃圾回收。沿用框架路由路径解析，覆盖配置了 root path 的部署。

版本、关键源码、原生引擎类型、serving 绑定与请求日志配置在启动时验证；活作用域和子请求有界，
清理失败保留本地占用。主项目没有新增 vLLM 依赖、业务路由、迁移或预算权威；插件默认关闭。

## 固定镜像安装与原生方法证据

本机既有 image ID：
`sha256:61cc94fec25198aeefcd93b96088a8e8db106d61d834a36d3b89ba7bf5b10765`。
镜像只在本机作为 `local/qwen3-asr-vllm` 存在，不将这个 ID 当作已发布的公共 OCI 引用。

从最终 overlay 源构建的 wheel SHA-256：
`64ff72db8c0c6d2ae80bf6acbe58a620d4bbc21f134698b10b8c5e7e6796073b`。
逐文件核对 wheel 内 Python/JSON 与当前源码相同，不含 pyc/cache。它是本机验证制品，不宣称已上传。

在无网络、无 GPU device request、只读根文件系统、2 CPU / 6 GiB / 256 PID / 512 MiB tmpfs
的独立临时容器中离线安装该 wheel，执行随仓库提供的 `probe_native.py`，退出码 0：

- 正式 entry-point loader 在未 allowlist 时不加载，在 render-only task 时不加载；
  transcription allowlist 才加载；实际 attach middleware，无新增路由。
- 实际导入并验证版本和 11 个关键文件摘要，使用原生 `AsyncLLM.generate/add_request/_add_request`、
  `ParentRequest`、`OutputProcessor.abort_requests` 与 collector close。
- n=1 和 n=2 两个受控场景中，均先发生一个可能已接受、回执未返的 add；取消前端后不抢先 abort，
  原 add 返回后按内部 ID 清理。n=2 的迟到 sibling 没有外发，parent/前端映射清空。
- n=1 刻意移除前端映射，最终 core abort 仍包含登记的原内部 ID；collector 的合成输入任务由
  原生 close 实际取消。两例本地待清理作用域均为 0。

IPC、前端 request state 与输入任务是受控协作者；没有模型、真实音频、实际 ZMQ 调度器或 GPU
工作。不能据此声称真实语音执行、中止、资源释放或集群资格通过。最早一次容器命令使用不存在的
`python`，退出 127；改用镜像 `python3` 后运行，失败原日志保留，不计为通过。

最终临时容器已删除。35B 服务持续 running，StartedAt 仍为 `2026-10-23T03:49:18.877205405Z`。
原始日志、镜像/容器配置、wheel 摘要和校验结果仅在本机
`.runtime/probes/b03-native-asr-scope/`，没有将运行数据或 vendor 源码入库。

## 验收限制与继续方向

同步 `fd60556` 后完整非集成 **3553 passed / 81.65%**，1192 集成参数另选，9 个既有 warning
保留；本轮不新增 skip。22 个作用域/真实 ASGI consumer 用例包含其中。Ruff format/check、
mypy 500 源、7 条依赖边界、契约与绑定检查、项目格式检查以及五个 Python wheel 构建通过。
没有持久化变更，不重复本地数据库全套；精确 PR CI 仍需覆盖最终提交的既有双库分片。

确定性测试覆盖提交取消/异常、语言检测与分段、partial fanout、跨请求隔离、前端映射消失、
清理失败、有界拒绝、普通/带前缀部署和流式 send 断开。完整验证与精确 PR CI 以具名 Issue
及 PR 最终 head 的报告为准，不能用旧基线结果替代新组合。

本地 cleanup 完成、core abort 消息发出、collector 关闭都不是停止/资源释放双证据。
下一步仍要核心执行屏障及批次/延迟释放事实、受限事实协议与网关消费者、真实模型完成/取消/故障、
有效参数和最终制品发布。TTS/Omni 多阶段独立核验，A 独占预算释放事务，unknown 继续占用。
B00–B07、父工作包、Gate 与 Goal 均保持原完整范围。
