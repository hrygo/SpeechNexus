---
title: "B03 真实 ASR 取消后的隔离、重放与整代次结算"
status: in_progress
version: "0.1.0"
date: 2026-10-25
---

# B03 真实 ASR 取消后的隔离、重放与整代次结算

2026-10-25 UTC（北京时间 10 月 26 日），在 #168 候选
`21129b4960e51e4704c4de50de3c520d78dc3aff` 上验证真实 ASR 取消后的安全链：
**取消保持 unknown，原身份重放不写结果、不退预算；整代次停止双证据经默认网关登记后，
由 A 原事务幂等结算租约和实例基础预算。**

本轮没有获得关联原生请求的 abort 直接回执，也没有获得单请求 GPU 停止/释放证据。
最终报告明确保留 `overall_status=partial`、`passed=false`，驱动退出 1；
只有 `cancellation_safety_chain_passed` 等实际完成的局部断言为 true。
不能把指标归零、局部安全链或文档 CI 当作单请求停止、生产准入、公开链或集群资格通过。

## 固定条件与实际装配

- 复用[整代次停止联验](2026-10-25-b03-real-stop-settlement.md)的默认控制器、
  默认文件 ASR 工厂、默认恢复循环和 A 原事务端口；没有注入产品 service、ledger、
  recognizer 或 controller runtime，没有修改产品代码、契约和预算权威。
- 固定镜像 image ID 为
  `sha256:61cc94fec25198aeefcd93b96088a8e8db106d61d834a36d3b89ba7bf5b10765`；
  vLLM 0.28.0，模型 revision 为 `7278e1e70fe206f11671096ffdd38061171dd6e5`。
  两次核对模型 10 个文件的大小与 SHA-256，均匹配既有 pin；旧校验文件保留。
- Linux ARM64 / GB10 单主机，Python 3.14.9、PyAV 19.0.2、NumPy 2.4.6、
  ONNX Runtime 1.31.0，固定 CPU VAD 摘要沿用前次报告。
  bf16、eager、单并发、最大长度 8192、KV cache 1 GiB，Memory/MemorySwap 均为 12 GiB。
- 使用内部 Docker 网络、不发布引擎宿主端口；节点走实际回环 TLS，网关走实际回环 TCP。
  存储使用一次性 Unix-socket-only PostgreSQL 和项目隔离 MinIO。
  本轮不宣称新增 InnoDB 或全产品多数据库验证。
- 输入仅为 19.424 秒合成英文语音，856,634 字节；没有读取私人语料。
  显式参数 `final_deadline_ms=10000`、`max_segment_ms=30000`，输出 token 上限 128。
  配置的调用上限与命令期限分别保留，实际策略取两者较小值。
- 就绪 provider、配方/Job 准入及 owner 过期仍是明确的 fixture。
  实例 base 12 GiB、单租约 slot 1 MiB 和一个 attempt 是隔离实验预算，不能注册为生产容量资格。

## 预检、失败与指标误判

最初的无模型预检失败，是实验脚本将 `final_deadline_ms` 写成 60000，超出现行源契约
10000 的上限。随后诊断保存了 `422 invalid_execution_request`；只修实验输入，
没有放宽产品校验。修正后有效请求返回 200，超出租约期限的负例仍为
`409 lease_not_active`，屏障/接管反例通过，私有 PG 确认停止。
更早那次未保存的 HTTP 响应，不补写成已经采集的证据。

三次真实运行均固定上述候选，失败没有覆盖为成功：

| 运行 | 加载及两次预热 | 实际结果 |
| --- | --- | --- |
| `8cca6a79a81f` | 163.071 秒 | 基线指标解析断言失败，尚未提交本轮执行。原始指标响应未保存；后续从固定镜像生成的格式样本复现旧解析器漏掉 `repetition` 序列，修正后无模型预检通过 |
| `d9020c19a3dc` | 160.902 秒 | 观察到 running=1，提交和两次取消均 200，取消保持 unknown；等待 abort 计数增加的条件 30 秒超时，后续重放/结算断言未执行 |
| `fd623296c624` | 135.728 秒 | 补完下节取消后的隔离、原身份重放和整代次结算；原生 abort 直接观察及单请求释放资格仍缺失，整体保持 partial |

固定镜像原生指标有五种 finished reason，包含 `repetition`；仅手写四种的合成样本不充分。
本轮在无 GPU、无网络、只读根的临时容器里，用实际枚举和 Prometheus 序列化器生成样本。
旧解析器在该样本上失败，修正解析器通过。这是观测工具修正，不是产品取消修复。

更关键的是，`vllm:request_success_total{finished_reason="abort"}` 不是主动取消的必要回执。
固定源码的 `AsyncLLM.abort` 先调用 `OutputProcessor.abort_requests` 移除前端请求状态，
再向 core 发送 abort；这个前端路径没有更新 finished-request stats。
迟到的 core 输出找不到原状态时被忽略，而指标 logger 从 finished-request stats 增加计数。
因此计数不增加，不能独立证明取消失效；计数存在也不证明 GPU 资源已释放。

部署内 `metric-counterexample.py` 从固定镜像源码提取未改动的
`abort_requests` 和 `process_outputs`，在合成请求状态上实际执行：
abort 输出入队、状态移除、迟到输出被忽略，finished stats 更新数仍为 0。
这个反例没有加载模型或运行完整原生引擎，只验证指标推断存在反例；
不能补成第二轮已经观测到原生 abort，也不能代替 GPU 停止资格。

## 最终运行的安全链与预算

1. 默认控制器真实 prepare/预热后，经明确 fixture 就绪/准入，
   默认网关接收完整 ASR 命令。原生指标从 idle 观察到 running=1、waiting=0，
   然后通过原 intent 的默认 HTTP 入口连续取消两次，合计 0.073 秒。
   两次均返回 `unknown_quarantined / quarantined / unconfirmed_timeout`、
   `cancellation_unconfirmed`，原 engine request ID 不变。
   这段时间是网关取消回执耗时，不是 GPU 停止延迟。
2. 随后指标 running/waiting 均归零；stop 计数仍为两次预热的 2，abort 计数仍为 0。
   这与上述指标限制一致，只记录观察，不据此签发停止事实或退预算。
3. 同一完整请求重放两次，均返回原身份与 `idempotent_replay`，状态保持 unknown。
   再次读取原生指标没有变化。递归枚举全部结果对象版本为空，staging 回执为空，
   停止事实表也为空；取消前后预算快照完全相同。
   A 提前 release 被 `execution_cleanup_pending` 拒绝。
4. A 原 quarantine 将 slot/attempt 转入隔离。未封锁的同 epoch stop 仍被拒；
   fixture owner 过期后，通过 A 原 acquire 取得 token 8，经实际网关屏障封锁旧 token 7，
   A 原事务持久化 stop，然后才向默认控制器发送停止请求。
5. 控制器整代次 stop 用时 7.274 秒，exit code 0；读回匹配 generation 的退出与资源释放双事实。
   A 原 observe 先保持 reconciling，不提前归还 base。
   默认恢复循环经 verified-stop GET，登记唯一停止事实，原执行成为 `aborted/complete`。
   这些动作仍没有直接修改 A 的预算。
6. A 原 release 结算 slot/attempt，重复调用 replay；再由 A 原 observe 结算 base，
   重复观察也 replay。两类重复操作均不再改变预算或 B 的停止事实。
   本轮没有任何私有结果对象被写入。

| 时点 | held 内存 | quarantined 内存 | slot held/quarantined | attempt held/quarantined |
| --- | --- | --- | --- | --- |
| 提交前及取消/重放后 | 12 GiB + 1 MiB | 0 | 1 / 0 | 1 / 0 |
| A quarantine 后 | 12 GiB | 1 MiB | 0 / 1 | 0 / 1 |
| 双证据登记、A release 后 | 12 GiB | 0 | 0 / 0 | 0 / 0 |
| A 最终 observe 后 | 0 | 0 | 0 / 0 | 0 / 0 |

控制器本轮只收到 prepare 和 stop 两次 POST，verified-stop GET 共 21 次，
包含驱动末次核验。只有一个关联真实执行，不能外推共享实例多请求终结或混合负载资格。

## 清理、证据与下一消费者

三轮均由原控制器停止目标，verified-stop 读回具有双事实；临时实例均为 0，
私有 PG 确认停止，桶由隔离 fixture 清理，网络删除成功，未使用应急直接 Docker 清理。
35B、embedding、reranker 前后运行且 StartedAt 未变；收尾再次核对全部三个运行的目标实例和网络均不存在。

原始证据保留在执行实验的默认 ASR worktree 忽略目录
`.runtime/probes/real-cancel-settlement/`。三个驱动分别冻结为 `probe-v1.py`、
`probe-v2.py`、`probe-v3.py`，报告内记录各自摘要；模型校验旧副本也保留。
没有将模型路径、临时凭证、音频或业务内容加入 Git。

| 证据 | SHA-256 |
| --- | --- |
| 首轮 `report-8cca6a79a81f.json` | `5fbbe63800c65b37b2efc95956fd9155591f4c8b08248754d8c3c040c812bd4a` |
| 次轮 `report-d9020c19a3dc.json` | `6167d0d08658bfaf84ba8d706830c903a3ed473ecac25ffc237bffeff9f44c3c` |
| 最终 `report-fd623296c624.json` | `224e1c62fa0b80db5e9a2bb8acc4104efe3df0b3443398ee3e90f3bac523286e` |
| 最终 `probe-v3.py` | `f07f24bdf946ba993779d38d907aeb0e6b057bfa123c94e883e56c4156e26027` |
| 无模型 `metric-counterexample.json` | `5791c872284b33e048b5d1379288dfe31c7c25ae78db2c372017b1faf7e4a01d` |
| 无模型 `metric-counterexample.py` | `38d35252fdb2a658421e9d63f2c7a4acc118993f4f3fcf46be18e21bb24b62b0` |
| 无模型 `preflight.json` | `1f40643f4956008151adc8462bede617be2e0a6d4932d7537cadcc77a096ccc7` |
| 最终 `model-verification.json` | `ab48ce27e3c4f6262d908e3442225bbc6af97c43cad8b2e7f68df6f15bd99358` |

A 可复用原取消状态、停止事实和结算次序接入生产准入/发布，不需要新增预算权威。
B 下一步须补关联原生身份的取消观察及可信单请求停止/释放，或按批准的整代次策略终结；
不能再把上述 abort 指标作为必要回执。正式就绪/批准来源、权威 binding/default、
实际 Job/调度、ASR 与 CustomVoice 公开链、故障/质量/容量及物理集群仍按原目标验收。
本报告不关闭 #28/#29/#30/#142、Gate 或 B00–B07 Goal。
