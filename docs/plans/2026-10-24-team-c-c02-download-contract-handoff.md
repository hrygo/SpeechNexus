---
title: "团队 C C02 固定版本文本下载契约候选交接"
status: proposed
date: 2026-10-24
---

# 团队 C C02 固定版本文本下载契约候选交接

[计划](2026-10-24-team-c-c02-download-contract-plan.md)将[固定版本导出用例](2026-10-24-team-c-c02-text-export-handoff.md)所需公开行为整理为[机器可读候选](2026-10-24-team-c-c02-download-contract-candidate.yaml)及可执行验收样例。C 是 workbench 契约 owner，A 是必须参与的评审方；本候选 `status=proposed`、`review_required=[A]`，没有 A 冻结或生产接线声明，[完整 goal](2026-10-24-team-c-goal.md)仍 active。

## 基线与候选行为

`base_sha = head_sha = 3380a76c89998b42be29b0c25e9f49443d23e15b`，当前分支 `codex/team-c-c00-versions`。上一累计 114 文件摘要已核对，fetch 无新提交。canonical workbench/speech OpenAPI `0.1.0.dev0`、bindings `0.1.0`、共享锁和生产源码保持原版；无提交、推送、外部消息或运行态变更。

候选只在测试内存中 additive merge 到当前 workbench OpenAPI：

```text
GET /v1/workbench/transcripts/{transcript_id}/revisions/{revision}/content
operationId: downloadTranscriptRevisionText
```

- 只读显式 revision，沿用共享 Identifier 和 19 位十进制字符串 Revision，不用浮点或默认当前 head。适用于 sealed 原文和 draft 校对稿，不改来源、任务、回执或已保存状态。
- 完整 UTF-8 文本，保留 BOM、换行、空白和 Unicode；空校对稿返回 200、0 bytes 和空 SHA256，不添加占位内容。最大 1,000,000 Unicode scalar values / 4,000,000 bytes。
- `Content-Length` / `X-Content-SHA256` 对原始未编码 bytes；媒体类型显式 UTF-8，内容编码缺省。消费者须先取得 bytes 验证，再严格解码，不能把替换损坏字符的解码当完整性证明。
- 固定文件名 `speech-transcript.txt`、`Cache-Control: no-store`、`X-Content-Type-Options: nosniff` 和请求关联 ID。Range 不选择分段，返回完整 200；不广告 Accept-Ranges、不定义 206/304 或可复用缓存。上述成功头属于候选要求，未证明真实服务已输出。
- 沿用共享错误 envelope。操作内 `x-nexus-error-semantics` 明确 400 `invalid_request`、401 `authentication_required`、404 `resource_not_found`、503 `workbench_unavailable` 的 type/retryable，只有 503 可重试。新 namespace 扩展随 OpenAPI patch 一起保留，具体 code/status 映射仍待 A/C 冻结。内部数据损坏按安全的 503 处理，不携带正文或源错误细节。
- 已保存 revision 与源 Artifact 生命周期分离：源到期不影响已保存文本；删除接受后发起的新读取 404。已开始读取不保证强制中止或撤回。没有新 Job、导出 Artifact、独立副本、幂等命令或字幕时间戳。

## 验证范围

核验时间 2026-10-24（Asia/Shanghai），Python `3.14.9`。

- 33 项案例先因候选缺失失败，候选实现后通过；媒体类型/重复 charset/内容编码验收补充先 5 failed / 37 passed，完善测试 oracle 后通过；错误语义必须随 patch 保留的案例先失败，再将矩阵放入 operation namespace。最终本单元 44 passed。
- 测试在内存叠加候选后通过 OpenAPI validator，逐一确认原有路径未变；复用 shared schemas 验证标识、超 JS 安全整数的 revision、状态特定错误、未知字段和必需成功头。原有固定版本用例的空/Unicode/BOM/最大 4 MB 内容满足候选的归一化验收样例。
- 正负案例包含错误长度/摘要、损坏 UTF-8、NUL、字符/字节上限、错误媒体类型/重复 charset、内容编码、下载文件名、缓存/nosniff 及请求 ID。响应头按 OpenAPI 类型归一化后校验，不证明原始 HTTP header 序列化、大小写/重复头处理或真实 HTTP 输出。
- 内存变异分别移除测试专用 oracle 的 digest 和字节数核对，各对应负例失败，生产和测试源文件未被写入。它证明验收 oracle 能检出错误，不是生产 handler 的变异覆盖率。一次脚本调用因缺少项目 import path 失败，修正测试运行器后重新执行；该失败不算变异命中。
- 完整 `uv run --locked pytest tests/unit tests/contract -q`：1011 passed in 37.58s；format/lint 通过，mypy 214 源文件，7 条 imports kept。正式 `make contracts-check` 28 文件通过，`make contracts-drift` up-to-date；两者只说明原正式规范/生成产物无漂移，候选单独由上述测试验证，没有生成新 URL 的客户端。

```bash
uv run --locked pytest tests/contract/test_workbench_text_download_candidate.py -q
make contracts-check
make contracts-drift
```

本轮无数据库、浏览器或真实 HTTP/引擎/设备/GPU 验证；上一导出用例的 106 项隔离 PG/HTTP 与既有浏览器证据保留原范围。日志、变异与执行记录位于 `.runtime/team-c/c02-download-contract-*`。

## A/C 正式接入条件

1. A 参与评审 URL、错误 code/type/retryable、空稿、成功头、Content-Encoding/完整下载和删除重叠语义；按正式版本策略确定 OpenAPI/bindings 版本后，C/A 按所有权将 candidate patch 纳入 canonical，而不是并行维护两个有效规范。接纳时把候选测试迁移为正式契约验收；当前 additive merge 故意拒绝已存在的同名路径，不能在接纳后继续叠加历史候选。
2. A 生成新 path 和工作台 Project/Transcript/Revision/View/List/错误的 Python/JS 运行时入口，明确 path/header 解码与严格 bytes/UTF-8 消费方式，验证 operation namespace 中不能仅靠 JSON Schema 表达的状态/错误关系。当前 workbench runtime 仍只有 ProjectCreateRequest/ProjectPatch。
3. 接 HTTP/生成客户端和浏览器：“导入→校对/CAS→选择固定 revision→下载”。直接验证原始响应头、完整 bytes/摘要/UTF-8、Range 行为、请求/来源身份、存储失败、安全错误和删除竞争；先冻结再接 route。
4. ASR JSON 任务意图与当前 text/plain 导入仍需明确新意图版本或结构化内容 validator；保留旧 journal 意图，不把 JSON 当纯文本原文。字幕须另冻结声学时间段/版本关系、SRT/VTT 正负样例，不按文字长度造时间戳。

## 执行决定与交付

采用候选 + 测试，而非直接变更正式规范，遵守 C owner / A 必须评审的边界；代价是正式冻结、生成和 HTTP/UI 仍未完成。显式 revision、完整 UTF-8、固定文件名/缓存策略减少歧义，代价是 Range/条件缓存、自定义名与字幕需另行定义。复用共享 schema、仅在测试中建立归一化验收 oracle，代价是不能把它当成生产 wire validator。未编码 bytes 同时决定长度/摘要，代价是生产须排除该路径的内容编码并实际验证原始 HTTP。将错误矩阵放进 operation namespace，代价是通用 OpenAPI 校验器/生成器不会自动证明其语义，生产者/消费者仍须覆盖。

沿用 C02 批准范围、本会话 inline、手工 ledger/package 与一次独立审查；缺失共享模板以显式摘要/应用/index 核验替代，保留旧改动。图谱 coverage 返回未索引，结论限具名源码和测试，不创建索引。最终独立审查与累计包摘要见 `.runtime/team-c/c02-download-contract-progress.md` 和 `team-c-current.manifest.json`。

## 独立审查

一次独立只读审查未发现 P1/P2，独立运行 focused tests 为 44 passed in 3.60s。一项 P3 延后：归一化验收 oracle 允许额外 header，因此误加 `Accept-Ranges: bytes` 仍会通过；主代理以合成输入只读复现。候选本身明确禁止该 header，接传输验收时须补禁止项和负例，当前测试不能证明响应不会误广告分段下载。

审查未判断真实 HTTP 序列化、重复/大小写 header、Range/304 实际处理、生成消费者、安全错误映射和删除竞争。这些均保留为正式接线验收条件：候选给出预期，尚无该 route 的用户可用能力；若只凭本轮测试发布，实际传输、错误或删除行为仍可能不符合候选。审查没有运行服务、数据库或完整套件；主代理完整 1011 项结果仅覆盖上述 unit/contract 范围，不扩展为 HTTP/PG 证明。没有重要问题进入修复轮，不追加复审。
