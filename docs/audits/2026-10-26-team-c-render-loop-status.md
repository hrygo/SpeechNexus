# 有声书生成链路：实现与证据矩阵（2026-10-26）

本表只记录**当前代码与已执行检查**能证明的范围。合成测试、MockTransport、一次性 PostgreSQL 各自只证明其覆盖层，均不能替代真实 speech 服务、真实模型、对象存储或物理拓扑证据。**C07/T09 与任何 AC 均未通过。**

## 链路与证据

| 环节 | 实现位置 | 已执行证据 | 未验证 |
| --- | --- | --- | --- |
| 生成派发 | `workbench/interfaces/http_render_units.py`、`application/render_units.py`、`infrastructure/public_speech_render_jobs.py` | 路由/应用/适配器单测（含 202 成功、409、422、5xx/不可读→unknown、键重放）；契约 503 `outcome_unknown` | 真实 `POST /v1/nexus/jobs`；真实凭据与模型；真实 TTS 音频 |
| 消费触发 | `web/workbench/src/books/RenderUnitPanel.tsx` | 面板 24 项：派发参数、未知原键重放、generating→ready 轮询 | 真实浏览器端到端；真实网络恢复 |
| 终态结算 | `application/render_reconcile.py` + `PostgresRenderUnits.complete_unit/fail_unit` | reconciler 11 项；PG 结算/拒绝重复结算/扫描（一次性 PG 实跑） | 真实 Job 终态；真实失败原因展示；结算循环默认关闭 |
| 独立副本 | `application/render_copies.py`、`SavedAssets` 既有链路 | 副本应用 3 项；页面保存消费者 2 项；待补副本扫描 PG 断言；永久拒绝与瞬时失败分类 2 项 | 真实 MinIO 往返；真实 speech 制品；分类仅进程内、重启后重试一次 |
| 生成历史 | `GET /v2/workbench/render-units/{unit_id}/generations`、`WorkbenchRenderUnits.generations`、`render_generations_payload`、`RenderUnitPanel` "生成历史" | 路由 3 项（默认 limit、404、非法 limit）；面板 3 项（记录展示与可展开披露、读取失败不伪造、超出上限标注）；页面按契约上限一次读取最近 100 条；覆盖门禁 42/10 | 真实历史数据；多单元混合；超过 100 条时仅标注、无游标翻页 |
| 状态恢复 | 面板日志、原键重放、pending/unknown 提示 | 单元/面板测试覆盖未知、拒绝、存储失败 | 真实浏览器存储故障；真实 VoiceOver |

## 明确未实现（C 自有缺口）

- 固定清单、检查点与续作、版本关联质量记录；生成历史一次最多读取 100 条，超过时只标注、无游标翻页。
- 结算循环 `render_settle_interval_seconds` 默认 0，部署未开启时无自动结算。
- 副本重试只在进程内记住“来源已不存在”的永久拒绝；无持久退避或次数上限，重启后该类单元会再试一次。
- 人工验收：全状态键盘/焦点、360px、减少动态、真实 VoiceOver 未在该面板执行。

## 证据边界

- 单元与集成测试均使用合成数据；无真实音频、无真实转写、无真实模型输出。
- 一次性 PostgreSQL 探针只验证 SQL/事务语义，不构成集群或拓扑证据。
- 真实链路解除条件：A 侧提供可访问的公开 speech 端点与凭据、可用的 TTS 模型；运维提供隔离 MinIO。解除后需分别记录公开服务、真实模型、目标拓扑证据。
