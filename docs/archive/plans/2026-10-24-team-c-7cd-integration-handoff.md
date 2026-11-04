# 团队 C — 7cd18879 集成交接

核验日期：2026-10-24。上一轮是已交付237文件包 `6b9465fcfd633d2b5fef3835add61536c268610cde01778e2e0f195195f1fe77`，不是等待或无进展。本轮从 `3b8052040111cdd8af3a50aaf5b0e8b3922e1f42` 安全 FF 到 `7cd1887905dd57e8c647238416b6030049833e38`；当前 base_sha=head_sha 为后者，没有 C commit/push。完整范围见[团队 C Goal](2026-10-24-team-c-goal.md)，前一轮页面证据见[稿件编辑器交接](2026-10-24-team-c-c1-editor-handoff.md)。

## 实际吸收范围

| PR | 当前本地制品 | 证据边界 |
| --- | --- | --- |
| [#77](https://github.com/hrygo/SpeechNexus/pull/77) | Meeting、MeetingCreateRequest、MeetingSealRequest、MinutesCreateRequest、MinutesJob JSON入口及5个HTTP operation | 生成解析器/校验，不代表C handlers、会议页面、LLM或运行资格 |
| [#79](https://github.com/hrygo/SpeechNexus/pull/79) | 公开 realtime session/PCM 与 realtime_events union/转写子集 Python/TS parser | 离线解析与样例，不代表WebSocket、credit/gap/epoch状态机或真实ASR |
| [#80](https://github.com/hrygo/SpeechNexus/pull/80) | B有界文件解码/重采样与VAD输入代码、独立vendor锁和历史探针说明 | 本轮未运行媒体/模型或私人音频，不复述为C质量资格 |
| [#81](https://github.com/hrygo/SpeechNexus/pull/81) | A租约隔离窄端口、事务与只读查询、生成manifest入口 | 本轮未运行A双库协调集成，不代替调度/停止或GPU验收 |

生成入口与约束以 `contracts/bindings.json` 和 `packages/contracts/README.md` 为准。workbench源版本仍为 `0.1.0.dev0`，bindings manifest `0.1.0`；生成器使用上游 `datamodel-code-generator 0.86.0` / `openapi-typescript 7.13.0`，TypedDict模板1。C1已实现工厂仍只注册实际13项；生成HTTP入口扩到15条路径/18项，不注册虚构的会议业务。

## 保留与合并

1. 开工前更新 #35/#40/#41/#43 当前任务并逐字回读，核对旧237文件与补丁hash全部一致，真实index无staged改动。
2. 固定远端main7cd18879，核对四个已合并PR、69个上游变更文件；只有5个文件与C交付交集：两份README、两份workbench HTTP生成JSON、HTTP样例。
3. 隔离干净目标先应用无交集的C补丁；README执行三方合并；样例按唯一id进行三方合并，重复修改若不一致即停止，不使用覆盖或简单拼接掩盖冲突。357条样例保留双方语义。
4. 两份生成JSON由目标版本A生成器结合完整C源契约重新生成，未手改生成物或复制独立wire语义；同时核对所有其他上游/base文件未被重建改变。
5. 核对整包预演，再仅反向移除已备份的5处C补丁，执行 `git merge --ff-only`，应用已验证的合并增量。237文件实际结果逐字节匹配预演，其中232文件完全不变，双方README内容保留。未使用stash/reset/checkout或删除旧文件。

证据在忽略目录 `.runtime/team-c/integration-7cd188/`：`preflight.json`、`preflight-generation.log`、`result.json`、合并前/后5文件和可逆补丁。若FF前失败，原C补丁恢复并核验；FF成功后旧237文件包及全部源快照仍可用于人工核对，不能用回退覆盖后续并行工作。

## 本轮验证

- Python：`uv run ruff format --check .`（500文件）、`uv run ruff check .`、`uv run mypy`（371源文件）、`uv run lint-imports`（7项边界）均通过；`uv run pytest tests/unit tests/contract -q` 为 **2735 passed / 37.57s**，没有测试skip。该范围不包含完整integration。
- 契约：`make contracts-check` 通过28份源；`make contracts-drift` 通过。生成临时目录的formatter修正临时生成文本后精确匹配，不改仓库源码以掩盖漂移。
- Web：format、lint、typecheck、test、build五项通过；**1150项 = contracts736 + Admin79 + Workbench335**，没有测试skip。
- 隔离PG：沿现有 `workbench-pg-probe.py` 使用项目 `.runtime/` 新建 Unix-socket-only PostgreSQL18.6，**269 passed / 11.60s**，组合覆盖94.66%；含真实loopback HTTP及C版本/列表/删除、纪要任务等原有用例，不是新Meeting HTTP联验。探针停库后另用pg_ctl确认退出码3且无postmaster.pid；不访问同机共享库。
- 生产bundle浏览器：实际Chromium `156.0.8078.4`，4个上下文/7次写请求；原键/草稿刷新、unknown重放、显式冲突、历史校对、存储损坏/配额、禁用部署、键盘和360px均通过，出站0、页面异常0、生成契约处理错误0。报告 `editor-browser.json` 明确真实后端/模型资格=false；浏览器和preview正常关闭。
- `git diff --check`、累计包干净目标apply-check/apply、全部文件字节、本地文档链接和打包时真实index不变均按最终manifest核验。累计238文件，patch/manifest为 `.runtime/team-c/team-c-current.*`；本文件不嵌入自身包hash，具体校验值见manifest与issue。上一3b/237文件6b9465fc包完整保存在 `.runtime/team-c/team-c-c1-editor-before-7cd.*`。

## 剩余工作

C2/C3生成依赖现已交付并吸收，不再记为等待其PR合并。下一单元由C实现Meeting会话/封存与Minutes公开HTTP业务，消费这些确定入口并接已有持久版本/纪要任务；公开realtime状态机及浏览器消费继续独立实施，不以parser替代业务语义。A仍负责共享生产root、owner迁移顺序及真实部署接线。

删除页面、公开下载与Artifact导入、C03音色工作室、真实会议/提词/LLM、有声书独立保存/导出、内容网络/恢复、设备/Linux/GPU及全部目标资格仍未完成。C00–C08全部open，C03 planned、其余in-progress，完整Goal active；G0–G5不由C越权关闭。本轮无新跨chat消息、模型/设备/生产服务变更。
