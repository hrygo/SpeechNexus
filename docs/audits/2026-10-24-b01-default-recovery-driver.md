---
title: "B01 默认网关自动恢复：配置、生命周期与真实进程验证"
status: active
date: 2026-10-24
---

# B01 默认网关自动恢复验证

开工基线`22d50ee`，先整合main`9d927f5`，交付前再同步`cafc6b9`，保留A的ModelBinding草稿与HTTP交付。
本单元承接#123的可信节点来源和#124的连续扫描；完成的是默认进程自动驱动，
不完成B01整包、B00–B07、Gate或模型/物理集群资格。

## 行为与证据

- 新增`gateway_recovery` 1.0.0私有配置profile，原生成器导出Python/TS类型和非强制转换校验。
  18组共享正负例验证地址、范围、类型、必填和额外字段；JSON不插入默认值。
  节点凭据保持独立Secret；加载器拒绝重复键、非有限数、超限/非regular文件、
  非法CA和歧义binding key。配置错误在获取入口所有权/数据库之前拒绝，错误不带部署输入。
- `ExecutionRecoveryQuery`窄端口保留原观察服务兼容性；默认组合只复用原
  `ExecutionQueryService`只读快照，不新增生产fake引擎或执行请求。
- 有效配置自动启动单活线程，持续复用扫描服务与本地游标。逐行停止/单调预算不访问
  剩余候选；当前行在各调用自身期限内结束。停机join后才关pool、释放ownership；
  configured worker未存活时现有readiness失败。来源离线不等于模型不就绪或执行已停止。
- 实际独立Uvicorn网关进程、隔离PostgreSQL最小gateway角色、真实TLS/SQLite节点：
  两条共享实例执行先unknown；半份证明或坏节点凭据时多轮自动GET仍unknown，预算保持。
  SIGKILL网关、重启节点/网关后，不人工调用sweep，自动关闭原两条执行；
  dispatch仍只有原两次，网关只GET、不写预算；随后A原release事务独立归还预算。
  正常停机日志确认lifespan完成，进程锁可重新获取；测试日志不含凭据或配置路径。
- PG/InnoDB原查询/停止ledger另跑原生周期消费者，batch=1自动跨页关闭两条执行，
  A预算保持。InnoDB仍是相同SQL/领域规则的测试装配，不宣称产品MySQL支持。

进程测试首次失败是退出码断言：Uvicorn完成lifespan后恢复并重抛SIGTERM，返回-15；
最终断言同时要求-15和`Application shutdown complete.`，不能只接受退出码冒充安全停机。
最初命令漏设兼容基线、一次集成文件名写错，均未计通过，随后以正确入口执行。

## 本地验证

从仓库根执行；双库测试仅指向项目隔离实例，测试DSN由本机环境提供，不记录凭据。

| 检查 | 结果与范围 |
| --- | --- |
| `uv run pytest -m 'not integration' --cov --cov-report=term` | 3316 passed，81.11%；9项既有警告未隐藏 |
| 三组恢复/配置/周期单元 | 51 passed |
| 下列8个相关集成模块，PG/InnoDB | 122 passed，0 skipped；含2个实际网关进程和2个原生双库周期消费者 |
| `uv run ruff format --check .` / `uv run ruff check .` / `uv run mypy` / `uv run lint-imports` | 通过；476源文件类型检查，7项依赖边界 |
| `make contracts-check contracts-drift` | 29份机器源校验及同源生成漂移检查 |
| `CONTRACT_BASE_REF=origin/main make contracts-compat contracts-http-changes contracts-speech-changes contracts-coordination-requests contracts-coordination-results` | 五个已声明范围的兼容入口通过；不冒称完整协议包含性证明 |
| 四包构建与源码目录外安装消费 | 通过；新profile解析与TLS来源激活，依赖沿用锁定环境 |
| JS OpenAI SDK消费者 | 3 passed |
| `pnpm format:check` / `pnpm lint` / `pnpm typecheck` / `pnpm test` / `pnpm build` | 通过；contracts消费者924项，无失败/跳过，Web两入口合计5项 |

相关集成入口：

```bash
uv run pytest \
  tests/integration/test_gateway_recovery_driver.py \
  tests/integration/test_gateway_recovery_progress.py \
  tests/integration/test_node_stop_gateway_recovery.py \
  tests/integration/test_gateway_recovery_sweep.py \
  tests/integration/test_gateway_recovery.py \
  tests/integration/test_gateway_barrier_http.py \
  tests/integration/test_gateway_stop_evidence.py \
  tests/integration/test_gateway_observation.py -q
```

## 边界与剩余工作

没有新SQL、表、预算权威、迁移或CI删减；没有执行真实模型、使用私人音频或操作GPU容器。
35B容器持续running，StartedAt仍为`2026-10-23T03:49:18.877205405Z`。
该结果证明进程/配置/原事务的机械行为，daemon和原始dispatch仍是合成替身。
真实引擎dispatch/abort/fault、ready持续监控、A完整调度预算、流式/音色/对齐/分人、
混合负载与物理多主机仍待。不采用etcd或Redis权威，集群要求不降级。

CI、PR与合并状态由Issue #28/#6的当前单元记录跟踪，未运行的远端结果不计通过。
