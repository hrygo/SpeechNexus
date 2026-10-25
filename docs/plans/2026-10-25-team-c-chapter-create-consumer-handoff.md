---
title: "团队 C 交接：有声书章节创建补线（并修正消费者清点方法）"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 团队 C 交接：有声书章节创建补线（并修正消费者清点方法）

## 先纠正我自己的错误结论

第 66/67 轮我写过"44 条已冻结操作里只剩 3 条没有 Web 消费者"（`getSavedAsset`、`getTranscriptRevision`、`getVoiceRevision`）。**这个结论是错的**，因为那次比对只检查契约**路径**是否出现在 Web 源码里，不区分 HTTP 方法——同路径不同动词（例如 `GET /transcripts/{id}/revisions` 与 `POST /transcripts/{id}/revisions`）会被误判为已消费。

按"方法 + 路径"重做（并容忍 `.PATCH(\n "…"` 这类换行格式），真实结果是 **7 条**：

| 操作 | 路径 | 现状 |
| --- | --- | --- |
| `createChapter` | `POST /books/{book_id}/chapters` | **本轮补齐**（UI + 客户端） |
| `createRenderUnit` | `POST /chapters/{chapter_id}/render-units` | 仅测试侧真实 HTTP 使用，UI 未接（下轮） |
| `getChapter` | `GET /chapters/{chapter_id}` | 无 UI 消费者；本轮由浏览器用例的 HTTP 复核覆盖 |
| `getRenderUnit` | `GET /render-units/{unit_id}` | 无消费者；列表已提供同字段，暂时不需要 |
| `getTranscriptRevision` | `GET /transcripts/{id}/revisions/{revision}` | 无消费者（修订历史只列不读） |
| `getVoiceRevision` | `GET /voices/{id}/revisions/{revision}` | 无消费者 |
| `updateProject` | `PATCH /projects/{project_id}` | 无消费者（项目改名未接） |

单条读取类操作**不必然是缺陷**（`getRenderUnit` 的字段列表已给全），但 `createChapter`/`createRenderUnit` 是真实功能洞：书籍列表能开、章节能列，却**无法新建章节**，因此有声书流程在 UI 里根本走不通。

## 基线

- `base_sha` = `head_sha` = `4b08c396c0fe8fc43c7e85683e534befab6bb22b`（未提交、未推送）。
- 上一冻结单元 `C07-saved-asset-read-path-http-evidence`（478 路径，补丁 `c750aad0…`）。
- 本单元 `C07-chapter-create-consumer`。

## 契约语义（实现前先读）

`ChapterCreateRequest` 只需 `title`（1–256）/`ordinal`（≥0）/`text`（1–5,000,000）——**不依赖 A**；响应 `Chapter` 带 `book_id` 与 `text_revision`。带 `Idempotency-Key` 时按原键重放。

## 本轮改动

- `workbench-client.ts`：新增 `createChapter(bookId, body, key)`，断言返回的 `book_id` 与请求一致（服务端串书不得被显示成本书的章节）。
- `BooksHost.tsx`：在打开的书籍下新增"新章节标题/序号/正文 + 新增章节"表单；成功后把新章节并入列表并直接打开它；失败时给出诚实文案，**请求键保持不轮换**（结果未确认时必须能按原键重放）。
- **顺带修正**：书籍创建原本每次提交都用 `crypto.randomUUID()` 现取新键，与界面自己"请按原键重试，不要重新创建"的提示矛盾；改为键保存在状态中、**成功后才轮换**。
- 未配置语音底座的部署里，章节面板的已发布音色目录仍如实报"音色目录不可用"，不用虚构音色填充。

## 证据

| 证据 | 内容 | 结果 |
| --- | --- | --- |
| Web 单元 | 客户端：路径/方法/`Idempotency-Key` 正确，`book_id` 不符时拒绝；宿主：新增章节进入列表、**被拒后重试沿用同一键** | workbench **585 通过**（582+3） |
| 真实浏览器 | 生产包 + 真实组合根 + 隔离 PG：选中部署项目 → 创建有声书 → 打开 → 新增章节 → 页面显示"第 1 章 · 合成第一章 / 正文修订 1"；随后由**真实 HTTP** 复读：书目恰为该标题、章节恰为 `(标题, 0)`、`GET /chapters/{id}` 与列表**逐字段相等**、`text_revision="1"` | 通过 |
| 全量探针 | 该用例接入 `--browser` 门禁 | **398 通过**（397+1） |
| 门禁 | `pnpm format:check`/`lint`/`typecheck`/`test`（workbench 585、admin 118）/`build`、`ruff format --check .`、`ruff check .`、`mypy`（528 文件） | 全绿 |

制品：`.runtime/team-c/browser/real-runtime-chapter-create/`（`result.json`、`events.json`、`stderr.log`、`chapter-created.png`）。

## 两处过程修正（都是断言，不是实现）

- 探针把 `chapterPosts` 声明在 `const` 链里，赋值直接抛 `TypeError`；改为 `let`。
- 探针与 Python 测试都断言"零非 2xx"，但该部署**必然**产生一条 `GET /v1/workbench/voices → 503`（没有语音底座）。已把断言钉成"恰好是这一条"，并额外断言页面显示"音色目录不可用"——比"零失败"更强，也更诚实。

## 未验证与边界

- 章节创建只覆盖 **UI 路径**；章节正文的**校对/token 化**仍按原计划依赖 A 的 ASR 制品。
- `createRenderUnit` 的 UI 仍未接（需要已发布音色目录或显式 pin 输入，且要 A 的音色协议）；`getTranscriptRevision`/`getVoiceRevision`/`updateProject` 仍无消费者。
- 本机 Chromium 与 macOS 宿主不代表 Linux 后端或目标拓扑资格；真实语音、设备、生产迁移仍需 A/B 具名制品。

## 下一接入动作

1. `createRenderUnit` 的 UI：在无音色目录时允许显式输入 pin，或明确禁用并说明原因。
2. `getTranscriptRevision`：校对归档按 revision 读取，与"只列不读"的现状对齐。
3. `updateProject`：项目改名（含版本 CAS 与冲突文案）。
