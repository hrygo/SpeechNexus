---
title: "B01/B03 原生 worker 完成观察与后续双证据接入"
status: active
date: 2026-10-25
---

# 原生 worker 完成观察

承接 #28 / #30 和[请求作用域单元](2026-10-24-b03-native-asr-scope.md)。#132 已合入
main `2bd6acc`，本轮不重复请求取消修复，不关闭 B00–B07、Gate 或 Goal。

## 源码事实与实现边界

固定本机 ASR 镜像的 `EngineCore.abort_requests` 只调用 scheduler finish。请求字典为空、
unfinished=0 或暂停不证明所有批次完成。`UniProcExecutor.AsyncOutputFuture.result()` 才消费
异步输出；原生 GPU 输出通过 copy event 等待 GPU，随后释放其设备 tensor 引用。
多进程 worker 也先消费 `AsyncModelRunnerOutput.get_output()`，才送出成功响应。

worker 的请求状态和持久批次按后续 `SchedulerOutput.finished_req_ids` 清理；调度器删除请求
与 worker 删除引用不是同一个时点。encoder、KV、connector、未消费输出和重叠批次还有各自的
生命周期。因此本单元通过正式 `worker_extension_cls` 只增加具名的 worker 观察方法，
读取原内部 ID 对应的状态项，并在已无可见引用和待完成步骤时对实际 worker device 同步。
不改原生推理、取消、缓存回收或调度逻辑，不增加 HTTP 路由。

请求与响应以 wheel 内 `worker-fence.schema.json` 为唯一校验来源。请求有数量、长度和总字节上限；
响应绑定新 nonce、原内部 ID 集合摘要和实际 rank。未知布局、源码差异、同步失败或异常均不产生
可用 GPU 完成结论，错误不反射模型路径或请求内容。该方法只在部署模板显式配置扩展后存在；
安装 wheel 与原 endpoint plugin 都不会自动启用它。沿用固定镜像中的 jsonschema，不在线升级依赖。

**所有响应的 `release_evidence` 恒为 false。** 即使 worker 返回 observed，也只说明这次观察的
可见状态与设备同步结果；没有证明核心收到原提交、没有迟到 add、没有关联批次/传输残留，
也没有建立可信实例/generation/任期。不能转换为网关 `VerifiedStopEvidence` 或归还预算。
worker 方法在引擎进程内运行；网关仍不能直接访问引擎 IPC，也不向业务 API 暴露任意 RPC。

## 实施状态与验收（2026-10-26）

- [x] 固定镜像只读提取执行器/worker 源码及摘要，保留无网络、无 GPU、无模型的提取记录。
- [x] 实现 worker 扩展、机器契约、正负例和响应绑定校验，并按最终源码重建 wheel
  （`speechnexus_vllm_asr_lifecycle-0.1.0.dev0-py3-none-any.whl`，
  sha256 `d24ac8bea7b72141aac05d4869ab498fab65cfce7af0b7a4433721f8962e46cc`）；
  此前与源码不逐字对应的制品作废。
- [x] request 校验顺序修定后定向回归 68 项通过，覆盖迟到清理、未完成步骤、传输、同步失败、
  同步期间变化和其他请求。
- [x] 在固定镜像 `sha256:61cc94fe…`（vLLM 0.28.0 / `g2cf0a6915`）中以 `--network none`
  安装上述 wheel 并运行受控探针：21 份源码摘要通过，扩展经正式 worker 注入，
  `pending`→`observed` 转换、其他请求与 encoder 项保留、原生 future 事件顺序均成立，
  镜像自带 `jsonschema 4.26.0`；无模型、无 GPU 设备、无真实 IPC。
- [x] Ruff 格式/检查、mypy（811 文件）、7 条依赖边界与 32 份源契约校验通过。
- [x] 精确候选 `c231256` 的 [CI40247880863](https://github.com/hrygo/SpeechNexus/actions/runs/40247880863)
  9 项检查成功，数据库分片按引擎范围跳过；受控协作者与探针不计真实 GPU/模型资格。

本单元按[团队 B 新阶段目标](2026-10-23-team-b-goal.md#新阶段目标交付可恢复的真实文件语音闭环)
重新排序：保留候选资产，后续仅在它解除真实执行闭环的明确阻塞时继续扩展。
已实现、受控探针通过、主干交付和真实资格是不同状态；不以本单元完成替代文件业务闭环。

## 通向真实闭环的下一段

1. 在引擎内部组合原作用域 seal/add 终结事实、原核心接收/顺序记录、调度器资源生命周期、
   全部关联批次和所有 worker 观察。漏掉 rank、未知内部 ID、迟到 add、失败或重启都保持 unknown；
   不用全实例暂停替代请求所有权。transfer/异步输出按真实配方补证。
2. 冻结具名引擎事实生产者和网关消费者的认证协议，绑定部署身份、generation、任期、原执行、
   时间和证据摘要。B 维护事实语义，涉及共享生成器时交 A 串行集成。
3. 接网关原 observation/abort/record_stop 和恢复扫描，只由 A 原事务消费双证据归还预算。
4. 用批准的模型和合成输入验证真实完成/取消/确认丢失/崩溃，随后接有效参数、最终制品及 A/C。
   TTS/Omni 多阶段另验；单 GPU 多进程仅标逻辑集群，多主机要求保持。
