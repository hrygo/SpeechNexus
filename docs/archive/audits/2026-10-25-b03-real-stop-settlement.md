---
title: "B03 真实整代次停止、网关恢复与 A 预算结算联验"
status: in_progress
version: "0.1.0"
date: 2026-10-25
---

# B03 真实整代次停止、网关恢复与 A 预算结算联验

2026-10-25 UTC（北京时间 10 月 26 日），在 #168 候选
`5f72b20ae5ddef4347cae8116fa9ac880b122ad8` 上，用真实 Qwen3-ASR 1.7B、
CPU VAD、Docker、SQLite、回环 TLS/TCP 和隔离 PostgreSQL/MinIO 验证：
**真实控制器停止事实可由默认网关恢复驱动登记，再由 A 原事务分别结算租约和实例基础预算。**
停止证据到达前继续占用，重复结算不再扣减。

这补充了[前次默认入口实验](2026-10-25-b03-default-real-asr-probe.md)未接通的停止消费链。
本轮仍使用显式 fixture 就绪凭证、配方准入和 owner 过期故障注入；
不证明生产就绪 provider、业务调度/准入、单请求 abort、公开发布/下载或物理集群资格。
没有新增产品端口、预算账本或协调机制，也没有修改 A/B 产品代码来适配实验。

## 固定条件与装配范围

- Linux ARM64 / GB10 单主机；Python 3.14.9、PyAV 19.0.2、NumPy 2.4.6、
  ONNX Runtime 1.31.0。复用私有 vendor 环境，没有修改项目依赖。
- 固定镜像 image ID 为
  `sha256:61cc94fec25198aeefcd93b96088a8e8db106d61d834a36d3b89ba7bf5b10765`。
  模型 revision 为 `7278e1e70fe206f11671096ffdd38061171dd6e5`；
  本轮重新核对 pin 中 10 个文件的大小和 SHA-256，全部匹配。
  CPU VAD 使用前次报告的固定摘要，并由默认工厂加载时校验。
- bf16、eager、单并发、最大长度 8192，固定 KV cache 1 GiB，
  Memory/MemorySwap 均为 12 GiB。peak 与 steady 保持相等，不签发降额或容量资格。
- 引擎使用唯一模板/实例和内部 Docker 网络，没有发布引擎宿主端口。
  仅保留汇总推理计数，继续关闭请求、输出和 access log。输入是合成英文句，没有读取私人语料。
- 节点 `create_app(settings=...)` 使用默认 Docker/SQLite 装配、原生观察循环和临时 IP SAN
  证书，经验证 TLS 接收 prepare/stop；没有注入 controller service、runtime 或 fake daemon。
  网关同样使用默认文件工厂和默认恢复配置，经实际回环 TCP 提交/查询，没有注入模型或 ledger。
- A 侧调用实际 `PostgresRuntimeTransactions`、`PostgresOwnerTransactions` 和
  `PostgresLeaseTransactions`，使用隔离 schema 的非 owner 最小角色；
  B 使用实际网关数据库角色。PG 为一次性 Unix-socket-only 集群。
  这是 PG 产品路径联验，没有新执行 InnoDB 或完整产品多数据库资格。

就绪 provider 仍是显式 fixture：真实控制器返回 ready 后，先验证 A 在没有受信资格凭证时
拒绝接受 ready；再使用明确标为合成的 readiness receipt，允许继续研究停止链。
该 receipt 中的 qualification digest 不代表真实容量资格。
配方/Job 准入仍由 fixture 提供一个已保留槽位，不是 A 的生产 admission 消费者。
预算投影保持一致：实例 base 12 GiB、单个租约 slot 1 MiB、一个 attempt；
前者通过 A 的真实 create/observe 事务维护，后者的初始准入是 fixture。

## 失败记录与无模型预检

首轮 `1d80b00de937` 用时 141.885 秒完成加载及两次预热，但网关提交断言失败，
未进入结果与预算验收。原驱动给请求保留了约六小时的默认 deadline，而 fixture 租约只有
十分钟。该轮没有保存原 HTTP 错误码；随后在相同边界、不加载模型的反例中复现
`409 lease_not_active`，请求期限改为租约内后返回 200。不能把后续诊断写成首轮已保存的响应。

首轮由原生控制器清理，verified-stop 读回具有退出与资源释放双事实；临时实例、PG、
网络均已清理。失败报告和原驱动保留，没有改写为成功，也没有放宽租约校验。

无模型预检另外验证：同 epoch、未封锁旧入口时，A 拒绝创建隔离实例 stop；
显式注入 fixture owner 过期后，调用 A 原 acquire 取得 token 8，经网关原 HTTP 屏障
封锁 token 7 的入口，再由 A 原事务持久化 stop。预检没有产生真实停止事实或归还预算。

## 成功运行与实际结算顺序

成功运行 `d4746142f607` 的加载及两次真实预热共 **139.745 秒**；
控制器 stop 用时 **6.090 秒**，exit code 为 0。

1. A 原 create 事务先保留实例 base，再向默认控制器发送固定 prepare。
   fixture 就绪/准入后，默认网关接收真实 ASR；原生成功推理计数增加 1，
   MinIO 中仅有一个私有结果对象版本。结果回执可读，同键重放不增加推理。
   本轮未重新读回比对完整文本，文本质量仍以前次对应版本的覆盖为限。
2. 关闭并重启默认网关，读取相同 staging 回执，原请求重放得到 `unknown_quarantined`；
   推理计数不增加，停止表仍为空，预算投影不变。
   此时 A 的 release 返回 `execution_cleanup_pending`。
3. A 原 quarantine 将 slot 1 MiB 和 attempt 转入隔离，base 12 GiB 继续占用。
   未封锁的同 epoch stop 被拒绝。显式注入 fixture owner 过期，A 原 acquire 取得 token 8，
   网关实际封锁旧 token 7，A 持久化 stop 后才向控制器 POST。
   这里验证了实际入口屏障，未把过期注入称为真实 scheduler 进程故障。
4. 控制器实际退出/删除目标，SQLite 检查点提供匹配 generation 的双事实。
   A 原 observe 先保存该 stop，但保持 `reconciling`，没有提前归还 base。
5. 默认网关恢复循环仅 GET 控制器的 verified-stop；原 `record_stop` 登记一份停止证据，
   执行变为 `aborted/complete`。此时 A 的 slot、attempt 和 base 仍占用。
   控制器共记录 17 次 verified-stop GET，含驱动末次读回核验；
   只收到 prepare 和 stop 两次 POST。
6. A 原 release 结算 slot/attempt，重复调用返回 replay，预算不再改变且 B 事实未被改写。
   随后 A 原 observe 重新处理同一 stop，确认全部 lease/submit 清理后才归还 base，
   实例进入 `stopped/released`；再观察返回 replay。
   实验中的 held/quarantined host memory、slot 和 attempt 投影归零。
   原 staging 回执保持可读，结果对象版本仍只有一个。

整代次终结是对该 generation 全部关联执行的处理，本例只有一个真实执行；
不能据此声称共享实例的单请求取消已完成。本轮也没有测真正的网络分区、
提交回执丢失、控制器故障重启、混合负载或多物理节点接管。

## 清理与证据定位

成功运行的 verified-stop 已读回；临时实例数为 0，私有 PG 确认停止，临时桶经 fixture
清理，Docker 网络删除成功。35B、embedding、reranker 前后均运行且 `StartedAt` 未变。
两轮均没有停止这些受保护服务。

部署内忽略目录 `.runtime/probes/real-stop-settlement/` 保存两轮报告、两个固定驱动、
无模型预检、制品校验和摘要；原始证据保留在执行实验的默认 ASR worktree。
驱动须在相同依赖、固定制品、授权服务及隔离资源条件下使用，不能把本机路径当作通用部署入口。
没有把私有配置、临时凭证、模型路径或业务内容加入 Git。

| 证据 | SHA-256 |
| --- | --- |
| 首轮失败 `report-1d80b00de937.json` | `726cca42221229cd3afa36f010cec8ebd35a7710862b8b760d7ef87181c0628f` |
| 成功 `report-d4746142f607.json` | `10df55e5d8059d2e24c3e652ab79552358230e904899d008d4b38fb40db70b1f` |
| 首轮 `probe-v1.py` | `3fa06e123a4ce3b9d6461e57bddccd597d010c8852e7decf2e50d304c5e17165` |
| 成功 `probe-v2.py` | `1a9b7974f92f495799ca612c630ec7cbab69f8d694a0b14b164adc18677c5d98` |
| 无模型 `preflight.json` | `c8b9908680211c68de45a2f85ba47c4e2a04e96ce8a32f2d8ca1376130be7d8d` |
| 十文件 `model-verification.json` | `abfa867cbc7d6aa0d65f1785c736dac1935ecd85ef96d818c77bd1b5995ec9ba` |

#168 当前提交的 [CI449](https://github.com/hrygo/SpeechNexus/actions/runs/40217515487)
全 14 项成功，三个必需检查均挂在 `5f72b20`；实际测试组合为 `595386a`，
包含 main `39aeb419`。集成汇总为 1697 collected / 1675 passed /
22 项既有 PG-only 参数不适用跳过，跳过不计作通过。
首轮浏览器依赖安装超时与重试成功记录保留。
CI、上述真实实验及后续文档提交分别绑定各自版本，不能互相转挂。
收尾只读核对时远端 main 已前进到 `250cf1c`，#168 仍 open/behind；
CI449 不覆盖这个后续主干组合，合并前仍须按正常流程核对新组合检查，不自行合并或绕过保护。

## 下一消费者与剩余工作

A 可沿现有端口消费本次真实停止、网关证据与结算顺序，不需要第二预算账本。
下一步仍是正式就绪/批准来源、权威 binding/default、实际完整 Job/准入和发布，
以及同版 SDK/C 的公开链；B 继续补单请求取消、期限/迟到、确认丢失、真实故障与
CustomVoice 持久执行。物理多机、完整质量/容量和 B00–B07 保持未验收。
本报告不关闭 #28/#29/#30/#142 或 Gate。
