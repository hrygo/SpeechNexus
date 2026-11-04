# 团队 C 2874c839 集成核验交接

核验日期：2026-10-24（Asia/Shanghai）。团队 C 当前 `base_sha=head_sha=2874c839f81a52cd079654372785a627dbea59a0`，分支 `codex/team-c-c00-versions`。本轮吸收 #69–#71，保留完整 C00–C08 目标和全部本地交付；没有创建提交、推送或启用生产服务/模型。

## 集成事实与保留检查

- 开工前更新 C00 #35、C08 #43 的当前执行单元及任务清单，再核对191文件包 SHA-256 `dedcad4705007c9cfcd0952117a1bf1c2d5e95fbd77475115edbd596e237e694` 和全部工作区字节，未发现未知或暂存改动。
- 从 a98 快进到2874。上游与C仅重叠中英文README，改动位置不同。先在干净2874临时副本应用完整191文件包；实际快进时仅临时反向应用已备份的C README窄差异，快进后恢复相同C差异，不使用stash、reset或checkout覆盖文件。189个C文件逐字节不变，两份README与预检合并结果完全一致，保留A/B运行时/网关新段落。
- 上游包含命令正文受信清理、runtime observe/资格核验和输出区间/原计量事实事务。接收这些实现不代表真实引擎、设备停止或物理预算资格通过。
- 旧网关时钟失败已由上游为ledger/service/engine注入同一合成NOW修复。当前原样工作区不应用历史候选，完整unit/contract已通过；旧a98交接保留其原时点事实，不再是当前阻塞。
- 上游未改变依赖锁、Python/Node版本声明、AGENTS或Web源码；未重新安装依赖。实际Python3.14.9、Node26.11.1、pnpm12.14.0；Node满足项目主版本范围，但仍非.nvmrc精确26.12.1，没有擅自升级。

## 当前原样工作区验证

| 检查 | 结果与范围 |
| --- | --- |
| `uv run --locked pytest tests/unit tests/contract -q` | **1659 passed**，40.56s；无skip、无时钟候选，不包含真实外部集成资格 |
| workbench socket-only PG probe `--coverage` | **262 passed**，18.35s，无skip；含实际PG与既有adapter单元；combined statement+branch coverage93.80% |
| Python ruff format / ruff check / mypy / lint-imports | 426文件格式、316源文件类型、7项依赖边界及lint通过 |
| `make contracts-check` / `make contracts-drift` | 28份机器契约通过，生成无漂移 |
| Web format / lint / typecheck / test / build | 全部通过；contracts165、Admin79、Workbench286，共**530项** |
| Admin正式事件探针 | 6条HTTP/4条开发场景，水位/重读/旧身份/取消及360px；无pageerror/出站 |
| Workbench正式文件探针 | 上传修正、Asset hint刷新、原创建/取消键、水位恢复、46字节下载与blob释放；无page/handler error或出站 |
| Workbench正式TTS探针 | 42字节原文本、1秒48044字节合成WAV、原键/版本、刷新/试听下载/旧内容与URL释放；无page/handler error或出站 |
| 正式手动提词探针 | 410码点、位置80、显式恢复/键盘/焦点/360px，无麦克风或API调用、无pageerror/出站 |

浏览器实测 Chromium156.0.8078.4，使用已有正式bundle和合成loopback HTTP/文本/WAV；探针关闭自己持有的browser/context/server。没有访问真实设备、LLM、远端推理或GPU。探针只证明列出的浏览器行为，不证明完整产品或支持矩阵。

隔离 PostgreSQL18.6 集群 `.runtime/team-c/pg-gdvk5urh` 报告 `stopped=true`，另核对 `pg_ctl status=3`、无postmaster.pid。所有日志/JSON在 `.runtime/team-c/integration-2874/`，其中 `pg-terminal.json`、`checks.json`、`web-checks.json` 和各browser报告可复核实际结果。

## 上游CI证据

[PR #71](https://github.com/hrygo/SpeechNexus/pull/71) 的已测head `3491e60b925e16af5162865e7740b10983efb04e` 与合入2874的Git tree均为 `704c1054693cb63324ad2766ff1e7a0ecea06b6b`。[CI run40044523239](https://github.com/hrygo/SpeechNexus/actions/runs/40044523239) 五项检查均success，包含Python静态/覆盖率、PG/InnoDB/MinIO、Web、部署配置和Python汇总。该证据属于未叠加C补丁的上游树，不冒充C的双库或生产资格；C当前树另由上述本地检查证明对应范围。

## 契约、依赖与后续

speech/workbench公开OpenAPI仍 `0.1.0.dev0`。`contracts/bindings.json` 的workbench runtime roots仍只有 `ProjectCreateRequest` / `ProjectPatch`，没有public realtime profile；本轮coordination新增的是redact/append入口，不解除公开工作台/实时依赖。A已在 #19 记录分批交付：C1项目/稿件/删除、C2Meeting/Minutes、C3公开realtime。C消费同版生成物，不复制公开字段/默认值，不改A的流水线/共享迁移/组合根。

C06内部持久纪要、无正文intent/attempt/计量保留、源/取消/删除屏障见[其交接](2026-10-24-team-c-c06-tasks-handoff.md)。未知LLM请求仍阻止物理清理；需要后续有资格的停止对账入口，HTTP取消不代表计算停止。公开Meeting/Minutes/助手、独立保存导出、C03声音工作室、C07有声书、C04公开实时及全部真实设备/网络/混合负载/恢复资格仍需交付。

累计192文件包以2874为基线，保留上一191文件包，新增本交接并更新当前目标/索引。已完成干净基线apply-check/实际应用、全部字节、本地链接/锚点与打包期间真实index不变核验。补丁、完整SHA-256、逐文件manifest、干净基线apply/字节/本地链接检查记录在 `.runtime/team-c/team-c-current.patch` 及相邻manifest；本地包不是已发布的main代码。全部C工作包及Gate保持未完成，完整goal active。

[团队目标](2026-10-24-team-c-goal.md) · [上一集成](2026-10-24-team-c-a98-integration-handoff.md) · [C06任务交接](2026-10-24-team-c-c06-tasks-handoff.md)
