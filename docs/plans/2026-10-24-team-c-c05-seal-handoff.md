# C05 稿件封存与固定源交接

核验日期：2026-10-24（Asia/Shanghai）。本单元是会议保存与固定转写源的必要基础，不代表完整 C05/C06、会议页面或 G0–G5 已验收。完整 C00–C08 goal 保持 active。

## 基线与范围

base/head：`dc14cf8fb0a7250842a8a7697c6884d6d21b2004`。启动时累计 159 文件补丁 SHA-256 为 `f1d6e942f2c23b41ad092920b244d86cf39662ecaa6449bdc10cb6933fb0ffb4`，逐文件核对后保留。当前 speech/workbench OpenAPI `0.1.0.dev0` / bindings `0.1.0`，未改公开契约、生成流水线、锁文件、组合根或 Web 源码；上游 README 并行改动仍未自动合并。

生产修改限 workbench 的 domain/application 版本用例、PG adapter 和新增 v2 owner-only 迁移片段。测试与一次性 PG 探针、目标/索引/本交接随之更新。另修复既有合成 HTTP fixture 在客户端断开后的关闭清理，并新增两个确定性回归；不改变产品 HTTP、异常映射或传输逻辑。未 commit、push、发送外部消息，未访问共享数据库、真实内容、设备或模型。

## 行为

`Transcript.seal` / `WorkbenchVersions.seal_transcript` 仅接受当前 draft 和明确 expected_revision。以新 revision_id / number 追加 sealed 快照，source_revision 指向原 draft；原稿、校对和已有 sealed revision 的正文与状态均不更新。缺省 text 原样保留，显式空串保存空稿；支持 BOM、CRLF、emoji、组合字符和首尾空白。校验继续拒绝 NUL、孤立 surrogate、超长正文；新方法严格拒绝 bool/float/string 等非领域整数版本、越界版本、旧 head、溢出与不合法时间。Artifact/receipt 配对来源不变，不推断时间戳、分人或保存音频。

已有 sealed head 不重复封存；后续修改先创建 draft。带原键的重放先于 stale-head 检查，返回历史 sealed 快照，不跟随当前 head。`transcript.seal` 与 revise 独立 scope，省略正文和显式正文的 intent 分开。版本、head 和回执在同一短事务提交；失败/取消不留正文或回执，提交确认未知不自动重试。

`get_sealed_revision` 只读取明确历史版本，拒绝 draft 和不存在/已删除记录，不替换为最新 head。既有固定 revision 文本导出继续保真读取旧稿/校对/封存稿。该方法不启动会议、LLM、任务、网络请求或制品发布。

PG adapter 只接受领域 `previous.seal` 生成的准确 sealed 迁移，拒绝伪造来源/跳号/身份差异；seal 回执不能指向 draft 或首版导入原文。删除接受后、正文清理后分别验证新读取与历史重放均拒绝，回执不能复活被删正文。

## v2 迁移与接线

原 `version_schema.py` v1 与 deletion schema 保持原字节。新增 `sealing_migration_sql(schema)` 是 owner-only v1→v2 forward fragment：保留全部旧行和回执，允许后续 sealed revision 及独立 seal 回执；仍保留首版语音/手工来源规则、配对来源、历史引用、长度与不可变权限。

前提是原 v1 fragment 及其 PostgreSQL 自动生成的约束名；缺少/改名约束及重复升级会失败。A 须将该片段纳入其版本化、全局串行迁移，在显式 owner 事务执行，并按迁移账本核对 v1 前置状态；这不是任意漂移 schema 的修复器。实测中缺失后一个约束时，前面的 ALTER 也随事务回滚。运行角色没有 DDL 或旧正文 UPDATE 权限，不做自动升级；v1 下 seal 写入以确定的 `23514` 回滚。

没有自动 downgrade。接受 v2 sealed 数据后不能通过放松校验、删除 revision 或改旧状态恢复 v1；生产回退由 A 保留兼容读写版本或按预案恢复，先停止相关写入。该单元只在新建一次性集群测试，没有修改生产 schema。

公开 MeetingSealRequest/Meeting/Transcript 响应和错误的运行时入口仍缺失；本单元的方法是 typed domain API，不是 wire parser。C 后续会议元数据/要点/会话绑定、封存事务组合、实时 preview/final/辅助 revision、提词旧稿/旧 epoch 隔离及 UI 均需继续实施。C06 只消费固定 sealed 源的语义已有基础，LLM/纪要本身仍未实现，默认关闭与网络边界不变。时间段/字幕协议和真实语音/浏览器会议/Linux 部署资格仍待对应单元。

## 实测

Python `3.14.9` / PostgreSQL `18.6`（Homebrew），均为当前 Mac 实测。只使用合成内容、项目专用 socket-only 临时集群。

- 新领域/fake `31` 项 RED→GREEN；合成 HTTP fixture `2` 条确定性 BrokenPipe/ConnectionReset 关闭测试 RED→GREEN。
- 新 PG module `11` 场景：v1 拒绝；v2 升级保留旧历史/回执/权限；约束缺失回滚；同键并发和历史回放；seal/edit CAS；真实提交成功但确认丢失后原键恢复；项目/转写删除接受及 cleanup 后拒绝；伪造来源和 draft seal 回执拒绝；公开合成 HTTP 导入后封存仍保留来源配对、无额外下载。
- 最终一次性探针 `117 passed`，无 skip，workbench infrastructure 语句/分支合计覆盖率 `92.04%`。这是整个基础设施集合覆盖率，不是完整会议产品覆盖率。集群 `.runtime/team-c/pg-_441nn1_`，`tcp=false` / `stopped=true`，并检查本项目所有探针均无 postmaster.pid。
- 首次实现后探针 `113 passed / 3 failed / 1 error`：新测试参数使用了错误 DeletionTarget 形状和不存在的 requests 属性；另一错误是既有取消下载 fixture 的 wait_closed 未处理断连，分别查清并修正。后续 `116 passed`，再新增 draft seal 回执反例，观察 `1 failed / 116 passed` 后补约束，最终 `117 passed`。失败均不计通过。
- 完整 unit/contract `1219 passed`；ruff format `349` 文件、ruff check、mypy `250` 源文件、imports `7 kept`、canonical contracts `28` 文件及 generated drift 均通过。
- 进程内移除“已有 sealed 禁止再次封存”条件，`2` 条测试失败，原源码 SHA-256 前后保持 `1853b56c954b51ef3b3554fbcc017af2feb027c880c365ca34d7a6b13a3b8830`；正常进程 focused suite `33/33` 通过。
- Web 源码未变，复用上一轮 `454` Web 测试及真实 Chromium 文件/配音证据，不把它们扩大为本单元会议页面、设备或 Linux 验收。未运行模型、访问私有素材或改共享运行服务。

可复现命令（仓库根目录）：

```bash
uv run --locked pytest tests/unit/backend/test_workbench_transcript_sealing.py tests/unit/backend/test_workbench_source_fixture_cleanup.py -q
uv run --locked python scripts/dev-env/workbench-pg-probe.py --pg-bin /path/to/postgresql/bin --coverage --output .runtime/team-c/c05-seal-pg-final.json
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy
uv run --locked lint-imports
uv run --locked pytest tests/unit tests/contract -q
make contracts-check
make contracts-drift
```

独立只读终审对本单元 13 文件给出 Critical / Important / Minor 均为 `0`，独立运行 `33` 项 focused tests、9 个 Python 文件的 ruff 及 diff/hash 检查；无需修复或重复审查。终审复核了上述 PG 和全量检查日志，但没有自行重跑数据库与全量门禁。报告位于 `.runtime/team-c/c05-seal-review-result.md`。

累计补丁包含 `165` 文件：上一轮 159 文件中 `152` 文件保持原字节、`7` 文件修改，另新增 `6` 文件。已在干净基线上成功 apply 并逐文件比对全部字节，核对 `233` 个本地文档链接，真实 Git index 保持不变。最终补丁为 `.runtime/team-c/team-c-current.patch`，逐文件 hash 与补丁 SHA-256 记录在同名 `.manifest.json`；旧 159 文件包保存在 `team-c-c02-tts-host-dc14cf8.patch` 和对应 manifest。生成文件、契约、锁文件、v1/deletion schema 和 Web 字节保持原样。

原始日志、探针、变异和最终完整性记录位于忽略目录 `.runtime/team-c/c05-seal-*`。这些包与记录供当前工作树交接，不代替提交或正式集成。

## 裁决及成本

1. 在授权 C05 范围先落固定封存源及真实持久恢复，不注册缺少生成入口的公开路由；成本是完整会议页面和 HTTP 仍需接线，不关闭 C05。
2. 封存追加新 sealed revision，已有 sealed 通过原键重放或明确读取，改文先创建 draft；成本是每次封存多一版，换取不覆写原文/草稿。
3. 保留原 v1 fragment，提供 owner-only v2；成本是 A 须串行集成生产迁移，runtime 不能自动升级。
4. 沿用当前授权工作树、手工台账/补丁和 dc14cf8；成本是人工完整性核验及尚未吸收上游提交。
5. 修复测试 fixture 的已确认断连清理缺陷；成本是本单元增加 fixture 范围与两个单元测试，产品网络行为不变。
6. 保留 owner-only fragment 和明确 v1 前置条件，生产迁移账本、schema 核验、全局串行集成及回退交 A 完成；成本是生产迁移与回退仍须独立验证，指定约束失败不能证明任意漂移都可检测。
7. 提交未知证据限真实 PG commit 加进程内 driver 故障注入；成本是实际丢包、数据库故障转移、生产负载和跨节点恢复仍须独立验收。
8. 不授予公开 wire、完整会议/提词/实时/LLM/字幕/UI、真实模型/设备和 Linux 部署完成资格，整体 goal 保持 active；成本是其余端到端实现与真实验证继续保留。
9. 对其余 152 文件仅复用原字节未变所支持的限定证据；成本是最终集成及对应 Web/Chromium 验收仍需完成。本单元无新增 Minor，既有其他单元的暂缓项不因此关闭。
