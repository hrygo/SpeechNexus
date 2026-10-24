---
title: "B02/B01 节点可信停止事实到网关的验证"
status: recorded
date: 2026-10-24
---

# B02/B01 节点可信停止事实到网关的验证

## 代码与范围

开工基线 main `a1804694e9510badae5f0497db59422288ea46a3`。
本轮新增 runtime-control `0.1.0.dev1` 的 `getVerifiedInstanceStop`，
节点按原 stop 与本地 released 检查点核实来源；网关新增固定 TLS 来源，
复用 `InstanceTerminationEvidencePort`、恢复扫描、每执行证据 ID 和 `record_stop`。
没有改 A 预算实现、共享迁移、部署或 CI；`uv.lock` 仅由 uv 为网关新增现有 httpx 直接依赖元数据。
契约产物由原生成器生成，原 A submit/GET 客户端保持兼容。

三种证据分别计量：

| 验证 | 实际覆盖 | 未证明 |
| --- | --- | --- |
| 共享契约/单元 | 来源、绑定、时序、身份与传输错误规则 | 实际 daemon/GPU 和数据库事务 |
| 原生 TLS/SQLite/PG/InnoDB 集成 | 节点重启、两个网关执行、A 原释放事务与预算保持 | daemon/dispatch 为合成事实，非真实引擎资格 |
| 真实 CPU 进程探针 | Docker 退出/删除、SQLite 检查点、控制器崩溃重启、TLS 网关消费 | 执行输入为合成领域记录；未在该探针操作预算或真实语音模型 |

## 真实 CPU 故障探针

Linux ARM64、Python 3.14.9、Docker 29.6.2；使用已有
`python:3.14.9-slim-bookworm`，镜像 ID
`sha256:7e0522950230cad1ec100ada6e3898c22c6bc82851289b4bdf0bf77c1c829cd5`。
仅启动 sleep 容器，不加载模型、不申请 GPU、不发布容器宿主端口。
控制器与临时 TLS 服务都仅监听 loopback；临时自签证书作为信任锚并校验 IP SAN。

```bash
uv run python scripts/dev-env/node-controller-stop-recovery-probe.py \
  --port 18022 --gateway-stop-evidence
```

探针只在子进程包装原 daemon/store 调用，生产代码没有故障参数。
故障阶段使用原生产入口；首次恢复使用普通生产入口，随后用 uvicorn TLS 参数启动
同一生产 factory、HTTP 路由、SQLite 和 lifespan，再由原生网关来源 GET。
恢复和来源消费均不发送新 stop/reconcile；在证据未持久完成前，新 GET 必须返回404。
原 stop 精确重放与再次正常重启另行验证证明稳定。

| SIGKILL 时点 | 持久检查点 | 当时容器 | 重启到原 stop 成功 |
| --- | --- | --- | --- |
| stop 前 | targeted | running | 4.186 s |
| wait 退出后、写 exit 前 | targeted | exited | 0.415 s |
| remove 前 | exited | exited | 0.316 s |
| remove 后、写 release 前 | exited | absent | 0.414 s |
| 原 stop 成功回执保存前 | released | absent | 0.415 s |

五项全部通过，退出码均143；最终容器删除、遗留 `[]`。
每项在同库再次启动的 TLS 节点上取得稳定来源事实，覆盖两个同实例代次的合成执行；
它们的执行证据 ID 不同，来源摘要相同。恢复时间是单次观测，不是 P95 或 SLA。
35B 全程运行，`StartedAt=2026-10-23T03:49:18.877205405Z` 未变。

本机忽略目录 `.runtime/probes/b02-trusted-node-stop/` 保存输出与受测源码摘要；
`cpu-gateway.json` SHA-256：
`dc1ca09c954cc333485172b1e7c2fbcb4f90026186015ddc10b53df1fe9d2611`。
临时证书、凭据、模板、回执、进程和容器已清理，未纳入 Git。

## 契约和双库消费

新增18个共享 Python/TS HTTP 正负样例，831个 TS 契约消费者通过。
节点/网关专项63项单元通过；覆盖只读查询、缺失/半证据、旧成功但无检查点、
证明冲突、重复可信来源、认证/节点/代次、完整绑定/任期/时间、重复JSON、响应上限、
截断/压缩、慢滴总期限、TLS 配置及并发无排队。

新增12项原生 TLS/SQLite/网关与 A 事务联调全部通过：

- 两条提交确认丢失的执行保持 unknown，A quarantine 后总量继续占用。
- 只有退出但尚未释放时，网关不关闭执行，A 拒绝释放。
- 同库重启后由节点本地观察恢复原 stop，消费者只 GET，不再次 dispatch/stop。
- 网关分别关闭两条执行，不修改 A 预算；A 随后分别归还原 lease，重放不重复归还。
- 缺凭据、不信任 CA、错误 IP SAN 的负例均先建立有效检查点证明，再验证来源被拒；
  没检查点的旧成功回执单独验证，不能靠恰好没有证明掩盖 TLS/认证缺陷。

扩展 runtime/intent、gateway recovery/stop、lease release 的相关 PG/InnoDB 回归：
**354 passed、0 skipped，112.13秒**。PG 使用生产网关适配器与最小角色；
InnoDB 网关侧执行相同 SQL/领域规则的测试装配，不声明生产 MySQL adapter 已完成。

首轮完整非集成出现一项接口清单遗漏：旧测试只列原三个 runtime 路径。
已将新路径加入精确集合，保留旧断言、认证与 schema 校验，不放宽协议。
修复后完整非集成 **3087 passed，覆盖率81.53%，89.38秒**；另1080项集成被选择条件排除，
不计通过。Ruff format/check、mypy 442源文件、7条依赖边界、28份源契约校验、
生成漂移、3个 SDK 测试、831个 TS 契约测试与 typecheck、pnpm format均通过。
OpenAI兼容、四类HTTP已知结构、显式完整speech源、协调请求和结果五个独立比较入口通过；
它们各自保留报告中的未验证语义边界，不代表完整协议语义兼容证明。
PR/CI结果随交付回填。

## 未完成范围

默认网关周期驱动与部署配置、真实 ASR/TTS dispatch/abort 和同类故障、
ready 实例持续监控、完整 scheduler/lease/预算闭环、GPU 质量/容量及物理多主机资格仍待。
本单元提供可组合的可信来源；不能宣称默认进程自动恢复或公开语音服务可用。
集群要求不降低，不采用 etcd，不新增预算权威。B01/B02、Gate 和团队 B Goal 保持开放。

[设计、升级和回退边界](../architecture/trusted-node-stop.md)
