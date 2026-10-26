# 有声书生成链路：实现与证据矩阵（2026-10-26）

收尾基线：main `54daef6ae4792b69a0e5e8645b09240ff715082e`。本表区分已集成组件、原版本检查和未决审查；合成测试、MockTransport、隔离 PG/MinIO 各自只证明其覆盖层，不能替代真实公开 speech 链、真实模型或物理拓扑。**C07/T09 与任何 AC 均未通过。**后续由新负责人安排，详见[最终交接](../plans/2026-10-26-team-c-final-handoff.md)。

## 链路与证据

| 环节 | 实现位置 | 已执行证据 | 未验证 |
| --- | --- | --- | --- |
| 生成派发 | `workbench/interfaces/http_render_units.py`、`application/render_units.py`、`infrastructure/public_speech_render_jobs.py` | #197/#198 路由/应用/适配器测试与契约 503 `outcome_unknown` | 先外发后登记、同键异体/可变快照、锁前读/CAS/source、完整受理及查询身份审查；真实公开 Job/音频 |
| 消费触发 | `web/workbench/src/books/RenderUnitPanel.tsx` | 面板 24 项：派发参数、未知原键重放、generating→ready 轮询 | 真实浏览器端到端；真实网络恢复 |
| 终态结算 | `application/render_reconcile.py` + `PostgresRenderUnits.complete_unit/fail_unit` | reconciler 11 项；PG 结算/拒绝重复结算/扫描（一次性 PG 实跑） | 真实 Job 终态；真实失败原因展示；结算循环默认关闭 |
| 独立副本 | `application/render_copies.py`、`SavedAssets` 既有链路 | #202/#206/#211 副本/待补扫描与分类测试；#177/#179 及既有保存链有隔离 PG/MinIO 字节回读、源离线和拒绝边界 | 真实 speech 制品→副本完整链；SpeechSourceError 分类不匹配、最老批次饥饿/无持久退避审查；进程内拒绝集合不能证明恢复 |
| 生成历史 | `GET /v2/workbench/render-units/{unit_id}/generations`、`WorkbenchRenderUnits.generations`、`render_generations_payload`、`RenderUnitPanel` "生成历史" | #208/#213/#214 的最近 100 条历史/披露/ARIA；#216/#217 的 fake 参数转发；工作台映射 42/10 | SQL 全读后切片、跨单元公平、稳定游标翻页；转发 limit 不证明数据库读取有界 |
| 状态恢复 | 面板日志、原键重放、pending/unknown 提示 | 单元/面板测试覆盖部分未知、拒绝、存储失败 | useRef 请求键跨刷新丢失、已确认提交后读取失败误报未知、请求乱序/加载态/焦点审查；真实全状态读屏 |

## C 自有缺口与未决审查

- 固定清单、检查点与续作、版本关联质量记录；生成历史一次最多读取 100 条，超过时只标注、无游标翻页。
- 结算循环 `render_settle_interval_seconds` 默认 0，部署未开启时无自动结算。
- 副本分类只在进程内保存拒绝集合，且来源异常类型/捕获分类审查未结；无持久退避或次数上限，最老 LIMIT 批次可能长期占据扫描窗口。
- 外发前完整持久意图、同键请求与固定快照、CAS/source 一致性及受理/查询身份仍需反例验证；#215 是登记，不是修复。
- 人工验收：全状态键盘/焦点、360px、减少动态、真实 VoiceOver 未在该面板执行。

## 证据边界

- 单元与集成测试均使用合成数据；无真实音频、无真实转写、无真实模型输出。
- 一次性 PostgreSQL 探针只验证 SQL/事务语义，不构成集群或拓扑证据。
- 项目已有隔离 PG/MinIO 测试能力，副本/下载也已有往返证据；C 自有持久化、分类/公平与读取风险不以等待 MinIO 为全部阻塞。
- 真实链路仍需 A 的音频 ready、权威配置/Job/准入/发布，B 的适用固定模型/结果与停止事实，再由 C 同版消费；公开服务、真实模型和目标拓扑分别留证。
