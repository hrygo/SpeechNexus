---
title: "PR #701 返修（`57df4e90..9b39b5ab`）的定点复核"
status: active
version: "0.1.0"
date: 2026-11-14
---

# PR #701 返修的定点复核（`9b39b5ab`）

- **复核对象**：`9b39b5abb24ec9c813ad30d39968f1271f4b8383`（PR #701 返修，基线 `57df4e90`）。delta 4 文件：检查脚本、登记类测试、渲染分发单测、`docs/validation/http-contract-change-checks.md`。
- **前序**：本报告作者在 `docs/reviews/2026-11-13-tts-contract-narrowing.md` 提出 M1–M4，已合入 `a4bc09f7`。
- **工作区**：独立 worktree `/private/tmp/sn710-r2`（`--detach 9b39b5ab`）。未改产品代码、契约、测试；变异只在工作树内瞬时进行并即时 `git checkout --` 还原，未 commit、未 push。
- **方式**：静态读 delta + 独立攻击探针（不采信作者输出）+ 真 CLI 复核 + 契约回滚/负例回归 + 夹具同形性比对 + 抽查两项数字。

## 结论

**四项返修全部成立。M1 选择的「自证整段请求体」方向正确且已生效：我原来那三组搭车攻击现在全部 REJECT，独立复现、含真 CLI。但同时引入了一条新的误拒（M5）：休眠项在「登记不主张任何东西」的状态下仍会否决内核**已容忍**的请求体改动，且文档声称休眠项「neither filters nor rejects」——那句现在是假的。**

**可否合并：可以（无安全漏洞、无红门，3722 passed），但建议合并前用 1 行修掉 M5 + 调整 1 条测试断言**；否则 `0.1.0.dev18` 期间任何向 `/v1/nexus/jobs` 请求体新增可选属性的改动都会被这条 #690 登记项判红。

## 四项逐条判定

### 1. M1 自证换到整段请求体 —— 成立

- **攻击面闭合（独立复现，不看作者输出）**：三组「挪到同级变体」的攻击现在全部 `REJECT`，且报出两条 finding（内核原 finding + 登记项不匹配）：

  | 组 | 额外改动（叠加在 PR 契约上，保留登记项） | 独立探针结果 |
  | --- | --- | --- |
  | a1 | `VoicePreviewJobInput.text.maxLength 2000 → 100`（同级收窄） | REJECT：`the operation request body has 2 bound differences; exactly the registered one is required` |
  | a2 | `VoicePreviewJobInput` 新增属性 | REJECT：`another structural change exists in the same operation request body` |
  | a3 | `VoicePreviewJobInput.text.maxLength 2000 → 5000`（同级放宽） | REJECT：`2 bound differences` |

- **真 CLI 复核**（`/private/tmp/sn710-r2` 内只把同级 `maxLength` 改成 100）：`check-http-contract-changes.py --base origin/main --surface speech` → `Known HTTP structural checks: rejected`，脚本退出码 **1**（非 `tail` 的 0）。上一轮同一变异是 `passed` / 0。
- **回归不倒退**：`/v1/audio/speech` 同文件另一 operation 的收窄仍保留自己的 finding；改登记项旧值/缺 proof/指向别的 operation/契约回滚四项仍逐一 REJECT；shipped PR 本身仍 `passed` + 1 条批准。
- 「整段 body 恰好一处」的粒度与内核 finding 的粒度由此对齐，类注释与文档的主语义不再是空话。

### 2. 「整段 body 恰好一处」是否误伤合法场景 —— 确实限制了一次只能一处；可接受，但未记录

- **同一次改动里两处合法收窄永远无法登记**：构造「`TtsJobInput.text` 200000→4096 + `VoicePreviewJobInput.text` 2000→1000」两处真实收窄，分别用 1 条、2 条登记项试：**两次都 REJECT**（`2 bound differences`）。即该登记类的表达力是「每个 operation 请求体每次只能登记一处收窄」。
- **判定：不是安全缺陷，是可接受的 fail-closed 取舍**。理由：(a) 拒绝方向安全，没有任何改动被静默放行；(b) 替代路径存在（拆成两次改动，或在下一个版本期分别落地——版本对不同时登记项本就不参与）；(c) 内核只发一条整段请求体 finding，若要支持 N 处就得把「多条登记项与 N 处差异一一对应」做出来，属于按需扩展。
- **但必须记录**：`docs/validation/http-contract-change-checks.md` 与 PR 正文都没写这条限制，错误文案也只说「exactly the registered one is required」，不告诉操作者该怎么办。建议在文档该节加一句已知限制 + 替代路径。当前 `TtsJobInput` 只被引用一次（`referrers = 1`），所以不存在「同一组件两处展开导致差异计 2」的假阳性。

### 3. 测试夹具改成两个互斥变体 —— 与真实结构同类，不是「为通过而造」

- 夹具（`tests/contract/test_approved_http_request_bound_narrowings.py::document`）现在 request body 的 `application/json.schema.oneOf` 是 `[TtsJobInput, VoicePreviewJobInput]` 两个 `$ref` 变体；真实契约是 `$ref JobCreateRequest` → `oneOf` 六个变体。
- 用模块自身的 `_request_body_surface` 打出的指针对比：

  ```text
  夹具: content/application/json/schema/oneOf/0/$ref(TtsJobInput)/oneOf/0/properties/text
  真实: content/application/json/schema/$ref(JobCreateRequest)/oneOf/1/$ref(TtsJobCreate)/allOf/1/properties/input/$ref(TtsJobInput)/oneOf/0/properties/text
  ```

  机制（`$ref(<组件>)` 标记 + 以 `properties/text` 结尾 + 变体切片）一致；真实路径多了 `$ref(JobCreateRequest)` 与 `TtsJobCreate.allOf` 两层，且登记变体索引是 1。
- 判定：**同形性成立**——被修复的通道（同一请求体内第二个互斥变体的改动）与真实契约同类，四条新负例确实走到该通道；嵌套深度差异由真实契约端到端用例（`test_the_real_speech_narrowing_is_the_only_filtered_change` 等两条）覆盖，两者互补。唯一可挑剔处：夹具的 `SIBLING` 名字叫 `VoicePreviewJobInput`，其内部形状是简化的（真实该 schema 还要求 `voice_draft_id`/`draft_revision`），属命名借用而非等价声明，不影响判定。

### 4. M4 渲染单元两条用例 —— 成立，`calls == 0` 断言的是真东西

- `calls` 由 `httpx.MockTransport(handler)` 的回调自增，handler 只在真的发出请求时被调用，所以 `assert calls == 0` 就是「一条连接/请求都没发」。
- 用**与 `submit()` 逐字段相同的 body** 二分边界（自己复现，不依赖代码里 `parse` 的报错文本）：

  ```text
   4095 chars -> parse ACCEPT
   4096 chars -> parse ACCEPT
   4097 chars -> parse REJECT (Invalid JobCreateRequest)
  ```

  同时确认领域层接受 4097 字符的 render unit（`RenderDependencies.text` 上限仍是 200000），所以那条 `RenderSubmissionRejectedError` 只能来自 `submit()` 里包着 `parse("JobCreateRequest")` 的那一个 `try`——它是 HTTP 调用前唯一的本地拒绝点。配套的 4096 用例断言 body 原样提交，构成上界可用的反证。
- 残余小瑕疵（非阻塞）：`unit_with_text("中" * 4097)` 在 `pytest.raises` 块内求值，若构造本身抛同类型异常会假通过；实际不会（领域接受 4097，且 `RenderUnit.create` 不抛该类型），但与 4096 对照用例已足以钉住。

## M5（新发现，建议合并前修）：休眠项会误拒内核已容忍的请求体改动

- **现象**：收窄已在基线与当前两侧都落地（都 4096）后，向 `/v1/nexus/jobs` 请求体**新增一个可选属性**——内核明确容忍（单独跑内核：`kernel findings: []`，属闭标签请求体的已批准追加路径）——门却被这条休眠登记项判红。
- **真 CLI 复现**（`--base 9b39b5ab` 即两侧都 4096，工作树加一个可选 `speed` 属性）：

  ```text
  contracts/speech-openapi.yaml: 0.1.0.dev18 -> 0.1.0.dev18
    - contracts/speech-openapi.yaml:paths//v1/nexus/jobs/post/requestBody: approved request
      bound narrowing entry does not match: another structural change exists in the same
      operation request body
  Known HTTP structural checks: rejected            # 脚本退出码 1
  ```

  同一现象对 `requestBody.required` 翻转（内核同样 0 finding）复现；纯 `description` 改写仍 `PASS`。
- **根因**：休眠条件被收紧成 `(before, after) == (to_bound, to_bound) and not differences and shape_same`。保护意图（「休眠项不覆盖旁边的改动」）其实由 `continue` 本身就保证了——`continue` 不删除任何 finding。收紧只多出「登记项自己造一条拒绝」，于是把内核已判为兼容的改动也一并否掉。
- **与文档冲突**：`docs/validation/http-contract-change-checks.md` 写「the entry neither filters nor rejects」。当前实现在「请求体动了」时既不 dormant 又无内核 finding 可留，实际是**新增**拒绝，该句不成立。
- **已验证的修法（1 行）**：把休眠判断恢复为只比较已落地状态，不看旁侧差异：

  ```python
  if (before, after) == (to_bound, to_bound):
      continue
  ```

  我在临时副本上改了这一行并重跑全部探针：a1/a2/a3 仍 **REJECT**、shipped PR 仍 **PASS + 1 批准**、上面的可选属性场景变 **PASS**。需要同步调整 `test_a_dormant_registration_does_not_cover_a_sibling_change` 的断言（该场景内核仍发 `exclusive variants changed`，门仍红；只是不再出现「is not the registered field」这条由登记项自己造的 finding，改为断言 `approved == []` + `FINDING in findings`）。

## 另核两条

- **文档表述**：主语义已同步为整段请求体，与实现一致（「whole request body must differ by exactly that one bound」「shape unchanged everywhere」「`$ref(<component>)` marker」均逐条对得上代码）。**唯一不一致是上面 M5 的休眠句**，随 M5 修好后即为真。另：PR 正文「契约检查补上……」一节仍写「15 例」、条件 4 仍是旧的「基线与当前服务 schema 只允许差这一个上界」措辞，属上一次遗留的陈旧块（现为 19 例、整段请求体语义），建议顺手更新。
- **PEP 758 `except` 写法未触碰**：`git diff 57df4e90...9b39b5ab -- scripts/dev-env/check-http-contract-changes.py | grep except` 为空；新旧两版该行文本逐字节相同，只是行号从 912 移到 948（delta 在前面加了行）。

## 新绿（实跑）

| 命令 | 结果 |
| --- | --- |
| `uv run pytest tests/contract/test_approved_http_request_bound_narrowings.py tests/contract/test_tts_text_bound_consistency.py` | `25 passed`（19 + 6，与作者「19 例」一致） |
| `uv run pytest tests/contract -q` | **`3722 passed in 145.06s`**（与作者一致；首轮 8 failed 是我这个新 worktree 未 `pnpm install` 的 TypeScript 桥用例，补齐后全绿） |
| `uv run ruff format --check .` | `1822 files already formatted` |
| `CONTRACT_BASE_REF=origin/main make contracts-speech-changes` | `passed` + 1 条 `approved request bound narrowing`（基线 `4ea3f208`） |
| `pytest tests/unit/backend/test_public_speech_render_jobs.py -k "job_bound or unrepresentable"` | `3 passed` |

## 只能依赖作者陈述

1. PG 集成用例（`test_public_job_admission_http_pg.py` 等 92 passed）：本机 probe 未配置，未实跑。
2. `mypy --platform linux`(1446) 未重跑（只抽查了 `3722`、`1822`、`contracts-speech-changes` 三项）。
3. PR 正文所称 CI 全绿（head `9b39b5ab`，18 项 pass）：未用 `gh pr checks` 逐项核对本轮。
4. `test_workbench_versions_postgres.py` 抖动：本机仍 skipped，无法复现。

## 时间线

| 时间（+08:00） | 动作 |
| --- | --- |
| 06:51 | fetch；确认 `9b39b5ab`；建 `/private/tmp/sn710-r2`；读 4 文件 delta |
| 06:53 | 登记类 + 一致性 `25 passed`；确认 PEP 758 两行逐字节未变 |
| 06:55 | 独立攻击探针：a1/a2/a3 全 REJECT；回归四项全 REJECT；休眠项正常 |
| 06:56 | 真 CLI 复核同级收窄 → `rejected`、退出码 1 |
| 06:58 | 真 CLI 复核休眠期可选属性 → **`rejected`（M5）**；验证 1 行修法可同时保住攻击面与合法增量 |
| 07:00 | 抽查数字：`3722 passed`、`1822 files`、`contracts-speech-changes passed`；夹具指针同形性比对 |

## 边界

未改产品代码/契约/测试；变异只在 `/private/tmp/sn710-r2` 内瞬时进行并已还原（`git status` 干净）；未合并、未 push main、未改动 PR #701 状态。
