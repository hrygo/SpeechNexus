---
title: "B02 停止过程崩溃与证据恢复探针"
status: recorded
date: 2026-10-24
---

# B02 停止过程崩溃与证据恢复探针

## 代码、环境与范围

集成基线 `97e9a30399d04aacee84f623022ab50d70853a4d`；最终实测代码
`667078e3cbc6bd37139fad6de5d49f8ddf240fbb`，后续提交只更新文档。
Linux ARM64、kernel `7.0.0-1019-nvidia`、Python 3.14.9、Docker 29.6.2。
使用本机已有 `python:3.14.9-slim-bookworm`，固定镜像 ID：
`sha256:7e0522950230cad1ec100ada6e3898c22c6bc82851289b4bdf0bf77c1c829cd5`。

容器仅运行合成 Python sleep 进程，不加载模型、不使用私人音频、不申请 GPU、不发布宿主端口。
控制器只监听 `127.0.0.1:18022`，探针配置观察间隔 0.2 秒（部署默认仍为 2 秒）。
故障子进程运行生产入口/组合根/HTTP/lifespan，仅在原 daemon/store 方法前后暂停；
父进程观察到暂停标记后 SIGKILL，用同一模板、原请求和 SQLite 启动普通生产入口。
生产代码没有故障开关。

## 旧版反证

从 `381e6852abda8c54aefc6cde62abbccb526b8507` 提取旧节点包，核对实际被 Python 导入的
`docker_runtime.py` SHA-256 为
`d2af7e5c7a59bfea33088bdd2df6495e3cd508004a44137a81daa0c6ba487042`，
与该提交原文件相同。对旧包运行同一个新探针的 `after_remove` 场景：

- 原 stop 已记录 `pending`；容器已删除，旧库没有退出检查点。
- 杀进程并重启后只 GET；5 秒窗口最终一直为 `quarantined`，没有停止证明。
- 探针以 `stop_recovery_timeout` 退出 1；临时容器已清理，35B 启动时间未变。

这证明缺口是删除前丢失退出事实，不是通过放宽“absent 即停止”的规则修好。

## 最终代码的五个崩溃点

以下恢复时间包含控制器重启，只是五次单场景观测，不是 P95 或生产 SLA。

| SIGKILL 时点 | 当时持久阶段 | 当时容器 | 重启→成功 | 退出码 |
| --- | --- | --- | --- | --- |
| Docker stop 前 | targeted | running | 4.168 s | 143 |
| wait 返回退出后、写 exit 前 | targeted | exited | 0.412 s | 143 |
| 删除前 | exited | exited | 0.315 s | 143 |
| 删除后、写 release 前 | exited | absent | 0.413 s | 143 |
| 最终意图回执保存前 | released | absent | 0.413 s | 143 |

五项都满足：

- 崩溃前原回执仍为 `pending`，没有提前公开停止证明。
- 重启恢复期间只 GET 原 stop 身份，不再次 POST 或发送 reconcile 快照。
- 最终 `succeeded` 同时携带退出与释放确认，且容器 ID 与最初目标完全相同。
- 原容器已删除，原请求精确重放仍为 idempotent，证明与 `verified_at` 不变。
- 再次正常重启后同一证明保留；遗留容器均为 `[]`。

最后一项证明的 `verified_at=2026-10-24T17:18:28.086764Z`。
受保护 `cube-ccba-qwen36-35b-nvfp4` 全程运行，
`StartedAt=2026-10-23T03:49:18.877205405Z` 未变。
原 HTTP 加载期间崩溃恢复探针及直接生命周期探针也都通过，退出码 143、删除确认、零遗留。

## 验证与复现

最终代码全量非集成 **2,946 passed，覆盖率 81.40%**；相关 runtime/intent 集成
**198 passed、0 skipped**。新消费者场景在真实 TLS/SQLite/PG/InnoDB 上装配原生 Docker adapter，
但 daemon 和资格是合成事实：只有退出、容器未删除时 A 保留总占用；节点重启补齐双证据后，
由 A 的事务归还原基线。它不证明真实 GPU 或完整 scheduler 联合资格。

定向测试还覆盖：无 exit 的消失不产生证明、同 generation 替代容器隔离、另一 generation 不被误停、
固定容器被重启不沿用旧退出事实、三个持久屏障失败、回执 CAS 竞争、两连接单调写入、
重复/损坏 JSON、v1 升级不改原回执、未知 schema 拒绝和 COMMIT 失败回滚。
28 份共享契约校验、私有 checkpoint 机器契约正负例、生成漂移、公开兼容检查、
Ruff、mypy、7 条依赖边界及四个 Python 包构建均通过。

```bash
uv run pytest -m 'not integration' --cov --cov-report=term
# 先按本机开发环境装入隔离 PG/InnoDB 测试连接配置；凭据不写入文档。
uv run pytest -m integration tests/integration/test_runtime_*.py \
  tests/integration/test_production_runtime_intents.py -q
uv run python scripts/dev-env/node-controller-stop-recovery-probe.py --port 18022
uv run python scripts/dev-env/node-controller-http-probe.py --port 18021 \
  --ready-timeout-seconds 15
uv run python scripts/dev-env/node-controller-probe.py --ready-timeout-seconds 15
```

本机忽略目录 `.runtime/probes/b02-stop-recovery/` 保留 red/green、原两套回归及环境/命令范围元数据；
`verification.json` 保存各 JSON 摘要。最终 green SHA-256：
`35f37398b820b6f12fb22824f987f02628a3a0d06191b937fa26b21268119cae`。
临时模板、回执、暂停标记、进程日志、控制器进程和合成容器均已清理。

## 未覆盖

未加载真实 ASR/TTS，未验证 GPU 上述故障路径、物理多主机、分区/接管或模型容量/质量。
ready 实例持续监控、可信整实例事实接网关、网关恢复驱动、完整执行/lease/预算联调仍待。
外部 reconcile 的调用方 stop_evidence 与节点 fencing 作用域仍是独立缺口。
本记录只证明本节点停止检查点与进程恢复，B02/Gate/团队 B Goal 保持进行中。

[停止恢复设计与私有契约](../architecture/node-stop-recovery.md)
