# C00 C1 JSON消费者与7372778集成交接

核验日期：2026-10-24（Asia/Shanghai）。当前 `base_sha=head_sha=7372778364872a0a2f9a4ae51b191b2804eee3ea`，分支 `codex/team-c-c00-versions`。吸收已合入#72–#74，并消费A00-C1已交付的项目/稿件JSON生成入口；未创建C提交或push，未操作真实引擎、容器、模型或设备。完整C00–C08目标继续进行。

## 集成与接口消费

- 原198文件包 SHA-256 `6ea076deff17cb4d7651d5551c26341b4179ee3e58a55782a7030967ab68a643` 已在干净exact737基线预检应用，实际FF后全部字节与预检一致。196个C文件原样，两份README保留双方内容；无stash/reset/checkout或未知改动覆盖。之后新增本计划/交接、两个接口模块及两个测试，只更新目标/索引两份既有文档。累计204文件，原194文件hash不变，两份README与预检结果相同。
- #73将 `workbench` runtime roots从2个扩展到16个：项目请求/响应及列表，稿件创建/修订/响应/版本/视图/列表，status/error和共享删除四入口。公开speech/workbench OpenAPI仍 `0.1.0.dev0`。本轮实际消费8个项目/稿件请求响应root；不能将生成入口存在推定为全部业务消费者已接线。
- 新 `interfaces/transcript_commands.py` 先用生成的 `parse` 验证manual/speech_artifact请求，再调用既有WorkbenchVersions。保留原正文、空修订、明确历史源和原Idempotency-Key；公开Revision字符串转为领域int，Project metadata_revision仍是原JSON整数。原键重放、版本/意图冲突、缺资源和源不可用保持既有语义，没有自动重投、假源或云回退。Artifact/receipt源下载与验证由既有reader执行，发生在事务外。
- 新 `interfaces/version_views.py` 显式投影Project、Transcript/TranscriptSummary、TranscriptRevision及TranscriptView，返回生成的models并以同版 `parse` 最终校验。正文只进入选中Revision；metadata不携带正文。所有revision以精确十进制表示，时间必须aware并转换UTC；不把错类型revision经str转换为合法值。
- View保留所选历史revision与当前metadata head，不将历史正文替换成head；跨稿件、未来版本、伪造当前head、错来源或不一致时间拒绝。源码只读、无I/O，调用方仍须从真实应用用例取得可见、权威的快照，mapper不证明历史存在、删除可见性或Artifact资格。
- 三个TDD回归确认残缺/空SpeechSource可能通过公开nullable字段schema；映射层按领域完整来源约束拒绝，避免将损坏来源降格成manual双null。没有修改公共schema或生成产物。

两个模块位于 `backend/src/speechnexus_backend/workbench/interfaces/`；测试为 `tests/unit/backend/test_workbench_transcript_boundary.py`、`test_workbench_version_views.py`。domain/application/数据库核心原样，不依赖传输DTO；没有新增公开路由或独立wire类型/默认值副本。

## 当前组合工作区验证

| 检查 | 结果与范围 |
| --- | --- |
| 新两个接口模块TDD | 缺模块RED→GREEN；57 passed，语句/分支覆盖98.04%；命令模块26项/100%；残缺源3项RED→GREEN |
| `uv run --locked pytest tests/unit tests/contract -q` | **1902 passed**，26.73s，无skip，原样737叠加C所有本地改动 |
| `uv run --locked ruff format --check .` / ruff check / mypy / lint-imports | 443文件格式、327源文件类型、lint及7项依赖边界通过 |
| `make contracts-check` / `make contracts-drift` | 28份契约通过，生成无漂移 |
| Web format / lint / typecheck / test / build | 全部通过；contracts219、Admin79、Workbench286，共**584项** |
| 隔离socket-only PG probe `--coverage` | **262 passed**，7.58s，无skip；93.80%组合覆盖；独立检查pg_ctl状态3、无postmaster.pid |
| 正式Chromium四探针 | Admin事件6HTTP/4开发场景、文件上传/原键恢复/46字节下载、配音原键/刷新/1秒48044字节合成WAV、手动提词410码点/位置80/360px；均无pageerror或出站 |
| 隔离副本mutation | 去掉跨稿件View关联检查，测试1失败/22通过；真实源码hash不变 |

使用现有Python3.14.9、Node26.11.1、pnpm12.14.0、Chromium156.0.8078.4、PG18.6；Node满足主版本范围，仍非.nvmrc精确26.12.1，无擅自升级。上游没有改依赖锁或版本声明，未重新安装。PG集群 `.runtime/team-c/pg-zk4vbou5` 已停止，browser/context/探针自己的loopback server均随探针退出。

日志、JSON、RED/GREEN、mutation、冻结源码hash和终审包在本机忽略目录 `.runtime/team-c/integration-737/`，过程账本 `.runtime/team-c/c00-c1-progress.md`。一次fresh独立终审发现1项Important（DST fold时间顺序），无Critical或Minor。按一轮TDD修复：8个正反用例先失败，再全部通过；全部时间比较使用UTC瞬时值，View以实际序列化的UTC时间做范围检查，避免同一ZoneInfo墙钟比较。修后57项/98.04%、原样1902回归及静态检查通过，不再复审；只有version_views.py及其测试相对审查hash变化，另两文件原样，最终hash另存c1-postfix-sources.json。结构图谱对全部依赖文件复核仍project not found，按具名源码回退，未创建索引。

上述PG测试验证既有工作台事务/删除/纪要等路径，新JSON接口使用fake进行请求、映射和业务关联验证；没有新增C1真实HTTP/数据库联合资格。浏览器为既有正式bundle的合成loopback场景，没有调用新C1接口、麦克风、模型或GPU。最终时间修复仅影响新增Python响应投影和对应测试，PG/Web/契约/浏览器消费路径未变，复用同轮修前的对应证据。没有在本Mac运行上游全套InnoDB/MinIO/集群或B真实ASR探针，接受上游源码不等于C取得这些资格。

## 验证命令与交接

从仓库根运行：

```bash
uv run --locked pytest tests/unit/backend/test_workbench_transcript_boundary.py tests/unit/backend/test_workbench_version_views.py -q --cov=speechnexus_backend.workbench.interfaces.transcript_commands --cov=speechnexus_backend.workbench.interfaces.version_views --cov-branch --cov-fail-under=90
uv run --locked pytest tests/unit tests/contract -q
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy
uv run --locked lint-imports
make contracts-check
make contracts-drift
pnpm format:check
pnpm lint
pnpm typecheck
pnpm test
pnpm build
node scripts/dev-env/admin-task-events-probe.mjs --output .runtime/team-c/integration-737/admin-events.json
node scripts/dev-env/workbench-file-host-probe.mjs --output .runtime/team-c/integration-737/file-host.json
node scripts/dev-env/workbench-tts-host-probe.mjs --output .runtime/team-c/integration-737/tts-host.json
node scripts/dev-env/workbench-prompter-probe.mjs --output .runtime/team-c/integration-737/prompter.json
git diff --check
```

PG探针按[脚本说明](../../scripts/dev-env/workbench-pg-probe.py)显式提供本机既有PostgreSQL binaries目录；只创建本项目临时Unix-socket集群，不连接共享实例或安装服务。命令与结果仅代表实际测试范围。

累计包/逐文件manifest位于 `.runtime/team-c/team-c-current.patch` 和相邻 `.manifest.json`。旧198文件包单独保留为 `team-c-c07-render-2874.patch`；累计204文件包已核对干净737基线apply、全部字节、295条本地链接/锚点及真实index不变。本地包未发布到main，所有C工作包及Gate保持未完成，完整Goal active。

下一接入：收尾回读A00 #19确认，C1 HTTP的path/query/header/响应状态/错误及显式客户端验证已由[PR #75](https://github.com/hrygo/SpeechNexus/pull/75)交付，合入main `764279238b124a0ca31a07f2e11b428c6585031f`。本单元保持737冻结基线与上述实测范围，下一单元先吸收该具名HTTP制品，注册真实项目/稿件/删除路由并联验；迁移顺序/组合根由A协调。该新制品尚未由本轮C本地验证，不把A的CI当作C消费者通过。List分页游标、删除/status/error完整消费者，Meeting/Minutes与公开realtime生成入口也继续推进。C07固定依赖核心原样保留，持久化、真实保存副本、质量/试听/匿名确认和独立导出仍待交付，不等待C06助手。

[实施计划](2026-10-24-team-c-c00-c1-plan.md) · [团队目标](2026-10-24-team-c-goal.md) · [上一集成](2026-10-24-team-c-2874-integration-handoff.md) · [C07核心交接](2026-10-24-team-c-c07-render-handoff.md)
