---
title: "B02 外部快照与停止证明来源验证"
status: recorded
date: 2026-10-24
---

# B02 外部快照与停止证明来源验证

开工基线为 main `1e2f70a23700d6a58f1ab7fb6c78eb4938441881`；合前重基到 `e51a109`，
保留 #120 新增的 speech HTTP 门禁及文档。此前节点原 stop 的检查点恢复
已由 #119 交付，但 `POST reconcile` 仍会把调用方提供的观察和停止证明写入未决意图。
持组件凭据不意味着已核实物理停止；这条旁路能绕过节点自己的退出与释放检查点。

## 收窄后的行为

[机器契约](../../contracts/internal/runtime-control.openapi.yaml)保留 URL、operationId、
字段、必填性、schema 和错误信封。端点只比较调用方断言与节点持久记录，返回诊断差异；
它不写意图或检查点、不调用 runtime、不改变留宿内存和代次替换资格。说明与源摘要由
现有生成命令更新，没有手改生成产物或新增生成器逻辑。

快照里的双确认、容器 ID、退出码、时间戳均不能形成停止证明。`fencing_token` 只为 wire
兼容保留，不生成节点级任期高位；各资源池任期不跨池比较。`accepted_at` 取节点处理时间，
返回的 quarantine 名单不代表已持久隔离。完整事件语义、消费者与升级限制见
[证据来源设计](../architecture/node-stop-recovery.md#外部快照与权威证据来源)。

## 反证与回归

新增的 16 项来源测试在修复前全部因事实被改写、调用方时间被采信或诊断缺失而失败，
修复后全部通过。回归包括所有实例状态、伪造完整停止证明、同库重启、精确重放、代次替换门、
低/高快照 token 与另一个资源池，不加载模型或私人语料。

真实 loopback TLS、原生 HTTP/SQLite 与 A 的 PostgreSQL/InnoDB 消费者另验证：

| 场景 | 实际结果 |
| --- | --- |
| 加载中上报 ready、absent、stopped，带未来时间和高 token | 原回执/版本不变；GET 仍为 loading；A 保留 peak |
| 原 Docker adapter 已取得 exit，删除尚未完成；上报正确容器 ID/退出码与伪造释放 | 检查点仍为 exited，意图仍隔离；无新增 stop/delete；总占用保持 |
| 消费同一原意图的未变回执 | A 返回 replayed，数据库快照与占用不变 |
| 同库重启后由节点恢复原 stop，实际补齐删除与释放 | 检查点单调完成；GET 出示双证据，A 才归还基线 |

daemon 和 readiness 资格在这些集成测试中为合成事实，TLS、应用、SQLite 和双库真实运行。
这不是实际 GPU 停止、ASR/TTS 故障恢复或物理集群资格。

## 本地验证

- `uv run pytest -m 'not integration' --cov --cov-report=term-missing`：
  **3006 passed**，覆盖率 **81.38%**，92.85 秒。
- 隔离 PostgreSQL/InnoDB：
  `uv run pytest -m integration tests/integration/test_runtime_*.py tests/integration/test_production_runtime_intents.py -q --no-cov`：
  **204 passed，0 skipped**，70.78 秒。
- `uv run ruff format --check .`、`uv run ruff check .`、`uv run mypy`、
  `uv run lint-imports`：通过，7 条架构边界保持。
- `make contracts-check contracts-drift contracts-sdk-test`：通过；
  五项独立兼容命令通过（包含重基后新增的 speech 全源检查），只证明各命令明确覆盖的结构范围。
- `pnpm format:check`、契约包 `typecheck` 与 `test`：通过，TS 消费者 **813 passed**。
- 35B 容器持续 running，StartedAt 保持 `2026-10-23T03:49:18.877205405Z`。
  本单元没有新增容器、模型、数据库迁移、预算权威或运行端口。

本地日志放在 `.runtime/probes/b02-reconcile-source/`，不入库。PR/合并 SHA、远端 CI
和最终交接状态回填 #6、#29，并交接 #28/#22；没有在此记录尚未发生的 CI 成功。
所有服务入口必须完成升级才能依赖收窄后的语义；旧事实不能追溯变成物理证明。
真实引擎故障、ready 持续监控、可信整实例事实接网关、周期恢复和完整执行/lease/预算闭环
仍待交付，B02 与原生 Goal 保持开放。
