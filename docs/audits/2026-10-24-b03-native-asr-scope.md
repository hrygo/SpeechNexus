---
title: "B01/B03 原生 ASR 请求清理交付边界"
date: 2026-10-24
---

# B01/B03 原生 ASR 请求清理交付边界

对应 #28 / #30，[实施计划](../plans/2026-10-24-b03-native-asr-scope.md)与
[独立 overlay](../../deploy/engines/vllm-asr-lifecycle/README.md)。
最初基线 `fcb0670`，实施期间 ff-only 同步 A #131 至 `fd60556`，既有制品完整保留。
P2 修复期间正常合并 A #133 的主干 `a56a2d5`，保留其默认参数存储、迁移与测试。

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

P2 修复后候选 wheel SHA-256：
`09b23cbcb42986a773e31f8ed1b068ce8b8141159d9aac266bc9b678ad6e2c6f`。
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

## 管理 P2 与独立原生取消路径回归

管理 review `5640704449` 锚定首版 head `303bf61`，发现 facade 只包裹 generate，
仍透传 serving 的外部 ID abort；原生外部映射会展开不同作用域的同名请求。首版单请求探针及
22 个测试没有覆盖这条路径，不能作为跨请求隔离的完整证据。

本轮先用新增单元反例复现取消第一个请求时错误发送第二个内部 ID。随后在固定镜像用同一探针
脚本（SHA-256 `39d24524d9851ff3b32ea00a5c4a3663f4a805e98712b89a395548a831d51f11`）
实际调用原生 `SpeechToTextBaseServing._detect_language`，包含原生异常取消处理：

- 首版 wheel `64ff72db8c0c6d2ae80bf6acbe58a620d4bbc21f134698b10b8c5e7e6796073b`
  在两个相同外部 ID 的在途请求中取消第一个，明确观察到另一作用域 ID 被发送；探针按预期退出 1。
- 修复 wheel 退出 0：facade 从本作用域外发前登记的 external/internal/parent 关联解析目标，
  等自己的 add 后才发取消；第二个 add 仍未返回，不会被等待或取消，之后通过原生生成/解析返回。
  重复取消和传入别人的内部 ID 都未扩大范围。
- 单元覆盖语言检测的单 ID 与 transcription 的 ID 列表、partial fanout/重复取消、
  未确认清理、原生非 scope 行为。没有禁止或改写用户外部 ID。

修复复用同一内部 ID 清理方法与 add 等待方法，没有另一份所有权账本或对共享外部映射的信任。
RED/GREEN 原始日志分别是本机 `p2-native-red.log` 与 `p2-native-green.log`；两次容器均删除，
35B running/StartedAt 未变。旧 CI `40145146801` 全 11 成功但耗时 329 秒（5 分 29 秒），
只属于首版，不能替代修复后验证；主要耗时是串行非集成测试 231 秒，构建 wheel 2 秒。

P2 修复在 `fd60556` 组合上的完整非集成为 **3558 passed / 81.71%**，27 个作用域/ASGI/原生取消语义测试包含
其中；1192 集成参数另选，9 个既有 warning 保留，没有新 skip。Ruff format/check、mypy 500 源、
7 条依赖边界、契约/绑定、格式与五 wheel 构建通过；随后 `a56a2d5` 新组合及最终提交远端 CI、
管理复审另行记录，不能用旧组合替代。

同步 `a56a2d5` 后重新完成 **3574 passed / 81.32%**，1247 集成参数另选、9 个既有 warning，
27 项作用域/取消测试均通过；mypy 511 源、其余静态/契约/构建检查通过。固定镜像 wheel 源码未变，
同脚本 RED/GREEN 证据仍对应当前 overlay。

IPC、前端 request state 与输入任务是受控协作者；没有模型、真实音频、实际 ZMQ 调度器或 GPU
工作。不能据此声称真实语音执行、中止、资源释放或集群资格通过。最早一次容器命令使用不存在的
`python`，退出 127；改用镜像 `python3` 后运行，失败原日志保留，不计为通过。

最终临时容器已删除。35B 服务持续 running，StartedAt 仍为 `2026-10-23T03:49:18.877205405Z`。
原始日志、镜像/容器配置、wheel 摘要和校验结果仅在本机
`.runtime/probes/b03-native-asr-scope/`，没有将运行数据或 vendor 源码入库。

## 验收限制与继续方向

首版同步 `fd60556` 后完整非集成 **3553 passed / 81.65%**，1192 集成参数另选，9 个既有 warning
保留；未新增 skip。首版 22 个作用域/真实 ASGI consumer 用例包含其中。Ruff format/check、
mypy 500 源、7 条依赖边界、契约与绑定检查、项目格式检查以及五个 Python wheel 构建通过。
没有持久化变更，不重复本地数据库全套；精确 PR CI 仍需覆盖最终提交的既有双库分片。

确定性测试覆盖提交取消/异常、语言检测与分段、partial fanout、跨请求隔离、前端映射消失、
清理失败、有界拒绝、普通/带前缀部署和流式 send 断开。完整验证与精确 PR CI 以具名 Issue
及 PR 最终 head 的报告为准；以上首版数字不作为 P2 修复后证据。

本地 cleanup 完成、core abort 消息发出、collector 关闭都不是停止/资源释放双证据。
下一步仍要核心执行屏障及批次/延迟释放事实、受限事实协议与网关消费者、真实模型完成/取消/故障、
有效参数和最终制品发布。TTS/Omni 多阶段独立核验，A 独占预算释放事务，unknown 继续占用。
B00–B07、父工作包、Gate 与 Goal 均保持原完整范围。
