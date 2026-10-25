---
title: "SpeechNexus C02 公开 Artifact 原文导入交接"
status: active
version: "0.1.0"
date: 2026-10-24
---

# C02 原始转写导入交接

本单元新增公开 Artifact 读取适配器和工作台封存原文导入用例，实际 loopback HTTP 与隔离 PostgreSQL 已联通。生产组合根、工作台 HTTP、真实语音服务及浏览器导入流程尚未接入，C02、C08 和[团队目标](2026-10-24-team-c-goal.md)继续未完成。设计和边界见[实施单元](2026-10-24-team-c-c02-transcript-import-plan.md)。

## 基线与所有权

`base_sha = head_sha = cbf4a8ca1cf808fbc5ff4b125f7ff1e271496995`，分支 `codex/team-c-c00-versions`；本轮 fetch 无新提交，原有 83 个 C 文件继续保留。仅修改 workbench 应用、基础设施、自己的测试/探针和文档；无 commit/push、外部消息、凭据或共享服务变更。实时事件生成入口仍未交付，本轮没有复制 realtime DTO/validator。

消费 A 的 `speechnexus_contracts.parse("Artifact", ...)` 和公开 speech API `0.1.0.dev0`；workbench 契约同为 `0.1.0.dev0`。`contracts/bindings.json` SHA256 为 `7fba8b209e634442621e30cd6277f984ca9547f1baee9883520d131fa6468c3c`。本轮没有修改任何契约、生成器、生成产物或锁文件。

## 实现语义

- `PublicTranscriptSource` 使用组合根注入的获准公共服务 `httpx.AsyncClient`，只发 Artifact metadata/content GET。端点和凭据不来自工作台命令，不跟随 redirect、不访问引擎、不 fallback、不关闭注入的客户端。组合根仍负责出站边界、客户端配置和并发预算，内网 URL 本身不证明已获出站资格。
- metadata 经生成 validator，再核对 id、workspace、receipt、ready/transcript、过期时间和 `text/plain` UTF-8；content 根据公开路由以 `application/octet-stream` 读取。JSON/verbose/SRT/VTT 不能直接按正文导入；ASR 默认产出 JSON，后续客户端须明确请求 `transcript_format=text`，或等待相应内容生成入口/解析支持。
- metadata 上限 64 KiB，原文上限 4,000,000 bytes、1,000,000 Unicode 字符；逐块计数和 SHA256，不依赖 Content-Length，不执行 Content-Encoding 解压。拒绝非完整响应、失配摘要/长度、非 UTF-8 和下载期间到期。默认总期限 30 秒、每次 HTTP 15 秒；关闭 response，取消传播；错误只含稳定本地 code，无正文、URL 或原始异常链输出。
- `WorkbenchVersions(..., source_reader=...)` 增加 `import_speech_transcript`。先在短事务读历史回执/父项目，网络在事务外；最后重新查回执并锁父项目，原子建立 Transcript、sealed revision 1 与回执。与手工创建共用 `transcript.create` scope，来源/标题不同会冲突。同键重放返回首次封存快照，来源失效或 reader 停用后也不重新下载；并发允许多个 GET，只发布一个身份。
- 父项目下载期间删除，最终锁读必须拒绝；删除后历史回执不能复活。最终提交失败或取消不产生半成品/回执。校对追加 draft，并保留 sealed 原文和 source_revision；源 Artifact 不被修改。receipt 校验只证明公开元数据的引用关联，不是签名或 GPU 执行资格认证。

## 本轮证据

核验日期为 2026-10-24（Asia/Shanghai）。

| 检查 | 实测范围 |
| --- | --- |
| 生成校验/读取与应用集中测试 | 应用 7 项、来源适配 35 项；先观察两个缺模块 RED，再实现 GREEN。深 JSON 负例在现有实现已能安全拒绝，没有为它额外改生产逻辑 |
| Python 格式/lint | 257 个文件格式、ruff check 通过 |
| 类型与依赖 | mypy 183 个源文件通过；7 条 imports 约束 kept |
| 隔离 PG/HTTP 探针 | 98 passed：59 项真实 PG 验证（其中 6 项为本轮实际 loopback HTTP + PG 流程）、4 项清理生命周期 host fake 和 35 项来源适配单元；无 skip |
| 覆盖 | workbench infrastructure 语句/分支合并 91.92%；来源 HTTP 适配器在该探针中 100%。覆盖率只说明列出的测试，不代表完整 C02 资格 |
| 总套件 | `uv run --locked pytest tests/unit tests/contract -q`：716 passed in 32.88s |

真实 HTTP 服务仅绑定 `127.0.0.1` 临时端口，使用合成中文正文、chunked 编码及跨 UTF-8 字符边界的 chunk。六条流程分别证明：sealed→draft→原始回执、阻塞下载期间 `pg_stat_activity` 工作台运行角色连接数为 0 且父删除胜出、并发只发布一个身份、下载取消不留回执可重试、删除接受/清理后的原键均不再下载或复活；NUL 原文在最终事务前拒绝且原键可重试。fixture 关闭客户端、listener 和自己的 handler，不使用共享 PostgreSQL 或真实语料。

最终探针为 `.runtime/team-c/c02-import-pg-probe-final.json`，核验 UTC `2026-10-23T19:49:15.549968+00:00`；集群 `.runtime/team-c/pg-cesslwuq`，PostgreSQL 18.6、socket-only、`stopped=true`。探针命令：

```bash
uv run --locked python scripts/dev-env/workbench-pg-probe.py \
  --pg-bin /opt/homebrew/bin --coverage \
  --output .runtime/team-c/c02-import-pg-probe-final.json
```

Mutation 在独立 Python 进程绕过摘要比较：同长度错误内容导致原负例失败（1 failed / 2 passed），说明测试不是只检查长度。`.runtime/team-c/c02-import-mutant.log` 留有结果；生产源码 SHA256 前后一致。本轮没有重复 Web/浏览器、真实语音服务、设备或 GPU 验证。

一次独立只读最终审查发现 1 个 P2：合法 UTF-8 的 NUL 原文不能写入 PostgreSQL text。修复前来源测试与真实 HTTP/PG 用例均失败；现已在来源适配器以 source_invalid 明确拒绝、关闭流、不进入最终事务，不静默清洗正文，回归通过。未发现其他 P1/P2/P3。本单元未扩大到既有手工文本/项目字段的字符策略，其他路径仍需分别验证。完整 goal 不因局部绿色检查而关闭。图谱 coverage 确认仓库仍未索引，使用源码 fallback，不宣称图谱覆盖。

## 后续接入

1. A00 提供 `TranscriptCreateRequest` / `Transcript` / `TranscriptRevision` / `TranscriptView` / 列表与错误的生成入口，C 再接公开工作台请求/响应。当前未手写替代 DTO。
2. A/C 组合根配置获准公共 speech client、认证/期限/并发与出站边界、独立 schema 迁移、服务开关及 HTTP；本单元不验证这些生产配置。
3. C02 补入浏览器任务结果→导入→校对→冲突/历史→文本/字幕导出；文本格式选择、JSON/分段内容和字幕导出契约仍需冻结，不能使用当前 adapter 冒充支持所有转写格式。
4. C08/A07 继续对象区和内容删除/独立账本/恢复水位及重放屏障联调；当前文本保存/删除验证不能证明完整备份恢复。

累计补丁与逐文件摘要仍为 `.runtime/team-c/team-c-current.patch` 和相邻 manifest；文件数、base、SHA256 和应用检查结果以最终 manifest 为准。
