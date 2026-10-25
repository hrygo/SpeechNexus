# C02 固定版本文本下载契约候选计划

> 执行：沿用 C02 已批准范围，writing-plans / executing-plans，本会话只形成候选和可执行验收样例，一次独立审查。A 是 workbench 契约必须参与的评审方；候选不等于 A 已冻结，不接生产。

**Goal:** 把固定 revision 文本导出的 URL、编码、空稿、头、错误、删除和完整下载语义变成可验证的机器可读候选，为后续 A/C 正式契约与生成消费提供明确输入。

**Architecture:** 日期命名候选 YAML 仅描述对现有 workbench OpenAPI 的 additive patch；测试在内存叠加到当前规范并验证，检查已有路径不被改变。直接复用现有 Identifier / Revision / Sha256Digest / ErrorResponse。生产源码、canonical 契约、bindings/生成产物和路由保持当前版本；候选经 A 评审后由 C/A 按所有权接入。

**Tech Stack:** OpenAPI 3.1 / JSON Schema 2020-12 / existing jsonschema+openapi-spec-validator / pytest。

**Spec:** [三团队所有权](2026-10-23-three-team-delivery-plan.md#21-共享资产只设一个维护者)、[C02 文本导出接入清单](2026-10-24-team-c-c02-text-export-handoff.md#a00c02-接入清单)。这不是 C02 全流程完成或正式发布。

## 决定与边界

- 提议 `GET /v1/workbench/transcripts/{transcript_id}/revisions/{revision}/content`，显式 revision，无请求体或新增幂等/Job/副本；响应是完整 UTF-8 文本。
- 200 保真空稿和 BOM/CRLF/Unicode；4,000,000 bytes / 1,000,000 字符。Content-Length 和 SHA256 对应原始 bytes；客户端先取 bytes 核对，再严格解码，不能用替换损坏 UTF-8 的解码方式。
- 固定下载文件名、no-store/nosniff；Range 不选择分段，始终完整 200，不广告 Accept-Ranges。没有自动 head、条件 304、缓存复用或永久导出 Artifact。到期 Artifact 与已保存工作台 revision 分开，删除接受后的新读取 404，重叠旧读取不保证撤回。
- 400 非法标识/版本，401 共享凭据，404 缺失/删除，503 工作台不可用；错误沿用共享 envelope，具体安全 code/type/retryable 按候选的具名例子冻结，不透露源错误或正文。无登录/RBAC 语义。
- 正负响应案例是测试专用验收 oracle，不作为第二套生产 wire validator。正式生产边界仍消费 A 的生成/共享校验。

## Review Focus

- patch 不覆盖现有 schema/路径，不把候选状态、结构通过或示例通过冒充正式冻结/HTTP 实现。
- 空内容的长度 0 和空 SHA256 保真，最大 Unicode 不能把字符数当 byte 数；错误摘要/字节数、NUL、损坏 UTF-8 必须失败。
- 原始/校对版本固定；不把 draft 当 sealed，不改变旧任务或下载源 Artifact，不引入时间戳字幕语义。
- 共享错误 envelope 和已有 Revision string 语义复用，不把本机 int/domain 类型当公开参数。
- 缓存/Range/删除边界与宿主实际能力一致；响应头和错误样例不得带内部 URL/正文/身份字段。

## Task 1：候选与正负验收

Files: 新建 `docs/plans/2026-10-24-team-c-c02-download-contract-candidate.yaml`、`tests/contract/test_workbench_text_download_candidate.py`。

Interfaces: candidate `status=proposed` / `review_required=[A]` / `canonical_contract` / `base_sha` / `base_contract_sha256` / `patch`。测试只在内存 additive merge，使用现有 shared refs；测试响应 oracle 从该 OpenAPI 读取全部 header/body schema。

- [x] RED：候选缺失、增加路径/不覆盖旧接口、OpenAPI 验证、原用例产物→候选、显式标识/版本及各类响应负例。
- [x] GREEN：完成候选与归一化验收样例，最大文本/空稿/错误源/完整下载，schema/checksum/UTF-8 各自明确覆盖范围。

## Task 2：审查和 A/C 接力

Files: 新建本单元 handoff，更新 docs/plans/README.md 与 team-c-goal.md。

- [x] 候选与 canonical 校验、Python format/lint/mypy/imports、完整 unit/contract；内存变异证明 digest/byte 边界的负例有效，不改变生产字节。
- [x] 一次独立只读审查，无 P1/P2，一项 P3 记录延后；明确 A 必须评审、正式生成/HTTP/UI、字幕和真实资格仍待完成。
- [x] 累计包干净基线应用/逐文件字节、旧文件摘要和 index 核验；goal 保持完整 active，不发送尚未授权的外部消息。
