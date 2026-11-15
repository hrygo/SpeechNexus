# 词汇放宽审批基线热修的非作者审查（PR #855）

## 结论（第三轮，覆盖前两轮）

**现在可以合（head `76a7359a`）。** 第二轮的两条阻断都真的解了：只 bump `info.version` 的树 `6 passed`；给另一处 503 码表加未登记 enum 码 → 红并由 `widening_not_approved` 点名该 finding。第一/二轮已验的性质（死审批、删 enum 条目、删 const 条目、回退 const 值、未登记 const 改动、fixture 换成当代契约）全部仍红；浅克隆 `--depth=1` 全绿；head 的 CI run `43739451588` 无 failure（23 个 job：21 success、2 按路径条件 skipped）。**零条必须修**，四条建议都不影响合入。

第三轮的两个新事实：

1. **`carried == table` 源侧够、方向侧过宽。** `inspect_sources(surface="speech")` 只比对 `contracts/speech-openapi.yaml`，`report["findings"]` 里不可能混进别的源，`startswith(SOURCE)` 足够（甚至冗余）。但过滤是「类别 + 源」，没有限定 `/responses/`：实测给请求体 `SpeechRequest.model` 加一个 `const`，该测试会以 `.../requestBody/application/json/properties/model: const value changed` 报 `widening_not_approved`——这不是响应词汇放宽，与 docstring「同一文件的无关改动是 PR 门禁的事」冲突。门禁对同类改动同样红，所以没有漏放，只是报错语义错位，属随修。
2. **不存在第三类词汇放宽，所以不窄。** 内核只产出两种值词汇类 finding：`const value changed`（方向无关）与 `enum values added`（响应方向；请求方向是 `enum values removed`），其余全是结构类。审批表用的正是这两类，`classes` 又由表推导，因此「表里将来出现第三类会自动被断言」。

**最关键的那条取舍可以接受，但不该说成「版本对仍有守护」。** 版本对在 main 侧的唯一消费者就是这条断言刚绕开的过滤器；PR 时门禁的基线是 `origin/main`（已含 #828 的全部放宽），那 27 条 finding 根本不产生，故版本对记错在 CI 上没有任何可见后果。实测把 27 条里除首条外的 26 条改成 `0.1.0.dev17` → 该文件 `6 passed`、门禁也 `passed`；27 条全改才红，而那是两条合成用例把 `next(iter(table))` 首条硬编码成 `0.1.0.dev18` 的副作用——**只有首条被偶然守住**。取舍成立：fixture 冻结在 dev18、当代契约版本会继续上涨，历史 27 条的 `to_version` 不可能再与真实比对版本对相等，把约束接回断言只会换一种方式再红（等价解耦）。建议改用两个低成本替代：冻结 `classes == {"const value changed", "enum values added"}`，并在注册表注释里写明版本对只对注册当下的比对有意义。

## 结论（第二轮，覆盖第一轮）

**现在还不能合：第一轮的两条阻断都真的解决了（浅克隆与 CI 全绿、`const` 漏洞补上），但第二轮又查出两处小修，都在测试文件内、合计约 10 行。**

- **必修 1**：`_finding_class` 对 `enum values added` 解析错（`rsplit(": ", 1)` 取到的是码表而不是类别名），导致「未登记的 enum 放宽」逃过残留断言——这是第一轮第 2 条要修的那类漏洞的**残余**，只是从 const 挪到了 enum。实测在真实契约里加一个未登记码，该文件仍 `6 passed`（假阴性）；一行改成首段切分后即红。
- **必修 2**：审批表把版本对钉成字面量 `0.1.0.dev18`，而 fixture 冻结在同一版；契约 `version` 一升（该行历史上每个特性 PR 都动），即便词汇毫无变化，该文件也会以 27 条 `approval_without_widening` 变红。实测仅把 `version` 改成 `0.1.0.dev19` 即 `1 failed`。

第一轮结论（下方「结论（第一轮）」）记录的是 `9ed6b740` 的状态，保留原样；第二轮证据见文末「第二轮审查」。

## 结论（第一轮）

**不能合。** 修法方向对（完整克隆上 6 条全绿），但实现在 CI 上**红**：PR #855 的 `Python: tests, coverage and build` 与 `Gate: Python tests and static checks` 均 fail，两条新测试都以 `subprocess.CalledProcessError`（`git rev-parse`/`git show` 拿不到 `f3e9e1b4`）失败。CI 是 `fetch-depth: 1` 的浅克隆，且 `push` 事件不抓任何基线，因此这个 pin 在原作业里不存在；照现状合入，`main` 仍红，只是错误从 `0 == 27` 变成 `bad object`。另有一处真实漏洞：残留 finding 只断言了 `enum values added` 一类，22/27 条 `const` 审批对应的「未登记放宽」无牙。

审查对象与核对时间（2026-11-16，Asia/Shanghai）：

| 项 | 值 |
| --- | --- |
| 仓库 | `/Users/hrygo/Documents/SpeechNexus` |
| 审查 worktree | `/private/tmp/sn858`（分支 `review/858-vocab-baseline`，基线 `origin/main`） |
| 基提交 | `c7e43f9a2926507578513029ee8d1ee07ebce688` |
| 被审 head | `9ed6b740131ae6ebb63d6714d77fd97bda0095d7`（`fix/855-vocabulary-baseline`） |
| 冻结基线 pin | `f3e9e1b4b5e5397fb2d4af7f59e06dc790614065`（`c7e43f9a` 的父提交） |
| CI 运行 | run `43735354609`（PR 事件） |

环境：`uv sync --all-packages --all-groups` 成功后用 `uv run --no-sync pytest` 执行；worktree 未装 `node_modules`。

## 一、真的修好了吗：完整克隆绿，CI 红，合入后 main 仍红

### 完整克隆内：两侧都对

`origin/main`（`c7e43f9a`，即当前 head）上原测试照旧红，报错与 issue 一致：

```
$ uv run --no-sync pytest tests/contract/test_approved_http_response_vocabulary_changes.py -q
.F...                                                                    [100%]
E       assert 0 == 27
FAILED tests/contract/test_approved_http_response_vocabulary_changes.py::test_every_entry_matches_a_finding_of_the_real_comparison
1 failed, 4 passed in 3.13s
```

PR head `9ed6b740` 上绿：

```
$ uv run --no-sync pytest tests/contract/test_approved_http_response_vocabulary_changes.py -q
......                                                                   [100%]
6 passed in 2.00s
```

### CI 内：两条新测试都因取不到 pin 而失败

PR #855 的 `Python: tests, coverage and build`（job `132876797235`）实际日志：

```
subprocess.CalledProcessError: Command '['git', '-C', '/home/runner/work/SpeechNexus/SpeechNexus',
  'rev-parse', '--verify', '--end-of-options',
  'f3e9e1b4b5e5397fb2d4af7f59e06dc790614065^{commit}']' returned non-zero exit status 128.
subprocess.CalledProcessError: Command '['git', '-C', '/home/runner/work/SpeechNexus/SpeechNexus',
  'show', 'f3e9e1b4b5e5397fb2d4af7f59e06dc790614065:contracts/speech-openapi.yaml']'
  returned non-zero exit status 128.
FAILED tests/contract/test_approved_http_response_vocabulary_changes.py::test_every_entry_matches_a_widening_the_current_contract_still_carries
FAILED tests/contract/test_approved_http_response_vocabulary_changes.py::test_the_frozen_baseline_predates_the_widenings_it_is_compared_against
2 failed, 5964 passed, 5966 deselected, 18 warnings, 262 subtests passed in 439.19s (0:07:19)
```

同 run 的 `Python: lint, types and contract compatibility`（job `132876797281`）通过——那三条 `make contracts-*-changes` 用的是 PR 基分支 `origin/main`，与这个 pin 无关。下游 `Gate: Python tests and static checks` 因之 fail，PR 现在是 `BLOCKED`。

### 根因：pin 在 CI 的检出形态里不存在

`.github/workflows/ci.yml` 的 pytest 作业（作业体第 204–213 行）与静态作业（第 296–305 行）都是：

```yaml
      - name: Check out repository
        uses: actions/checkout@...
        with:
          persist-credentials: false
          fetch-depth: 1

      - name: Fetch only the compatibility baseline
        if: github.event_name == 'pull_request'
        env:
          BASE_REF: ${{ github.base_ref }}
        run: git fetch --no-tags --depth=1 origin "+refs/heads/$BASE_REF:refs/remotes/origin/$BASE_REF"
```

即：PR 事件只多抓一个 `origin/main` **深度 1** 的 tip（`c7e43f9a` 本身），其父提交 `f3e9e1b4` 不在对象库里；`push` 到 main 时这一步骤根本不执行，连 `origin/main` 都没有。仓库的 CI 约束本来就写在这份文档里（`docs/validation/ci-validation.md:63`：`routing`「以 `fetch-depth: 1` 检出，本地只有合并提交本身，第一父提交按 SHA 抓取一次」），所以「pin 一个历史 SHA 然后直接 `git show`」在这个仓库里是已知不可行。

本地按真实远端复现（2026-11-16）：

```
$ git init -q . && git remote add origin https://github.com/hrygo/SpeechNexus
$ git fetch -q --no-tags --depth=1 origin '+refs/heads/main:refs/remotes/origin/main'
real main tip=c7e43f9
$ git cat-file -t f3e9e1b4b5e5397fb2d4af7f59e06dc790614065
fatal: git cat-file: could not get object info      → ABSENT
```

在这份浅克隆副本上跑该文件，得到与 CI 同一形状的失败：

```
$ /private/tmp/sn858/.venv/bin/python -m pytest tests/contract/test_approved_http_response_vocabulary_changes.py -q
FAILED ...::test_every_entry_matches_a_widening_the_current_contract_still_carries
FAILED ...::test_the_frozen_baseline_predates_the_widenings_it_is_compared_against
2 failed, 4 passed in 0.11s
CalledProcessError: ... 'show', 'f3e9e1b4...:contracts/speech-openapi.yaml' ... exit status 128
```

### 合入也不会把 main 修绿

`push` 事件没有「Fetch only the compatibility baseline」，检出只有被推的那个提交，`f3e9e1b4` 依旧缺失；本地浅克隆复现出的两条失败就是 `push` 作业的形状。所以这不是「PR 事件特有问题」，合入后 main 依然红。

## 二、两个方向的反例：一部分有牙，const 类没有

全部变异只在临时 worktree 内做，跑完 `git checkout --` 复原；未 push、未改产品代码。

| # | 变异（临时副本） | 期望 | 实测 |
| --- | --- | --- | --- |
| A | 插一条 `enum values added` 死审批（指向不存在的放宽） | 红 | **红**：`approval_without_widening` 逐条列出 |
| G | 插一条 `const value changed` 死审批（503 改成 504 指针） | 红 | **红**：同样被 `approved == set(_table)` 抓到 |
| B1 | 删掉一条已登记的 **enum** 条目（契约仍带该放宽） | 红 | **红**：`leftovers` 抓到残留 finding |
| B2 | 删掉一条已登记的 **const** 条目（契约仍带该放宽） | 红 | **绿**：`6 passed` ← 漏洞 |
| E | 契约里新增一个未登记 **enum** 码 | 红 | **红**：`leftovers` 抓到 |
| W1 | 契约里新增一个未登记 **const** 改动（`type: const` 改写） | 红 | **绿**：测试 `6 passed`，同树跑门禁脚本 `exit 1` ← 漏洞 |
| D | 同一文件只改描述文字 | 绿（范围收敛正确） | **绿** |

A（死审批，enum 类）：

```
E  AssertionError: {'widening_not_approved': [], 'approval_without_widening':
   [".../voice-designs/post/responses/503/.../code: enum values added: ['voice_draft_never_existed']"]}
```

G（死审批，const 类）：

```
E  AssertionError: {'widening_not_approved': [], 'approval_without_widening':
   ['.../voice-designs/post/responses/504/headers/x-should-retry: const value changed']}
```

B1（删已登记 enum 条目）：

```
E  AssertionError: [".../voice-designs/post/responses/503/.../code:
   enum values added: ['voice_draft_admission_contended', 'voice_draft_engine_unavailable']"]
E  assert [...] == []
```

B2（删已登记 const 条目）——**没有牙**：

```
### MUTATION B2: delete ONE registered CONST approval (contract keeps the widened const)
......                                                                   [100%]
6 passed in 2.04s
```

原因：删条目后 `approved` 少一条、`table` 也少一条，`approved == set(_table)` 依然成立；残留的 const finding 落在 `report["findings"]` 里，而残留断言只筛 `"enum values added"`。W1 是同一漏洞的另一种形态（真实契约里新增未登记 const 改动），同一棵树下门禁脚本反而**能**抓：

```
$ uv run --no-sync python scripts/dev-env/check-http-contract-changes.py \
    --base f3e9e1b4b5e5397fb2d4af7f59e06dc790614065 --surface speech
  - contracts/speech-openapi.yaml:paths//.../retirements/post/responses/503/.../type: const value changed
Known HTTP structural checks: rejected      (gate exit: 1)
```

所以 PR 描述里「未登记的放宽（删掉一条条目）→ 红」对 enum 类成立，对 22 条 const 类**不成立**；文件 docstring「a widening the table does not name is filtered by nothing and also turns this red」也只在 enum 上成立。

## 三、pin 会不会烂，以及 22 条 `const` 审批的死活

`f3e9e1b4` 的**历史**位置没问题：它是 `origin/main` 的祖先（线性历史 801 个提交，非孤儿提交；`git merge-base --is-ancestor` 通过），指向的 `contracts/speech-openapi.yaml` 存在（6043 行）。它会烂的地方是**可达性**：浅克隆里取不到（第一节），而这是唯一真正会触发的形态。

两类后续改动的行为：

- 未来再发生**enum** 词汇放宽、且未登记 → 红（实测 E）；登记后由表过滤、不红 → 正确。
- 无关改动（描述文字、其他类别）→ 不红（实测 D）→ 范围收敛是对的。
- 未来发生**const** 词汇放宽、且未登记 → **不红**（实测 W1）→ 错。

22 条 `const` 审批的结论要分成两件事：

1. **死活（entry 对应的放宽被回退）有人管**：`approved == set(_table)` 是集合双向相等，死掉的 const 审批会让 `table - approved` 非空而红（实测 G）。这一条不依赖残留断言，因此 22 条并未「没人管」。
2. **未登记 + 值被替换没人管**：
   - 未登记 const 放宽不红（B2 / W1），因为残留断言只筛 enum；
   - 审批键**不含值**：`const value changed` 这条 finding 文本只有指针和类别。在同一指针上把已登记的值再改成任意别的值，finding 文本不变、仍被表过滤。实测（H）把 `X-Should-Retry` 的 `enum: ["true","false"]` 改成 `enum: ["true","maybe"]`：

```
### MUTATION H: already-approved pointer gets DIFFERENT values (approval text unchanged)
......                                                                   [100%]
gate exit: 0
```

即**测试与门禁脚本同时绿**。这条是审批键粒度问题，属既有设计（内核 `scripts/dev-env/check-openai-contract-compat.py` 的 finding 文本不带新值），不是本 PR 引入，但它说明「登记值」并没有真的被守住。

因此对「`const value changed` 类别是否也要断言」：**要**，至少残留下限必须把它纳入；同时也应放弃「表里没写就会红」的笼统表述，或补上值绑定（见建议 4）。

## 四、第二条测试能不能被绕过

把基线改到审批之后（`_PRE_APPROVAL_BASELINE = c7e43f9a`）→ **两条都红**，防线有效：

```
FAILED ...::test_every_entry_matches_a_widening_the_current_contract_still_carries
FAILED ...::test_the_frozen_baseline_predates_the_widenings_it_is_compared_against
2 failed, 4 passed
```

第一条也红是因为集合相等本身就会失效（对比对象等于工作树 → `approved` 为空、`table` 27 条），所以第二条不只是唯一防线，但它的失败信息更直白（`contains voice_draft_engine_unavailable`），值得保留。

`_added_codes` 的正则确实抓全了所有码（不只有第一个）：

```
_added_codes(table) = ['voice_draft_admission_contended', 'voice_draft_engine_unavailable']
单项多码解析 === ["'voice_draft_admission_contended', 'voice_draft_engine_unavailable'"]
人工构造 === {'a', 'b'}
```

解析失败时 `assert _KNOWN_WIDENED_CODE in added` 会红，属 fail-closed，可接受。它的实际问题与第一节相同：`subprocess.check_output(["git", "show", ...])` 在浅克隆里直接抛 `CalledProcessError`，没有「fail-closed 但说明清楚」的信息，与「防空转」的初衷无关地红掉。

## 五、同类缺陷普查：没有第二处恒红

```
$ grep -rn 'inspect_sources("origin/main"' tests/ scripts/
（无命中；本文件已改为 pin 提交）
```

- `tests/contract/test_speech_http_changes.py`、`test_http_contract_changes.py` 中所有 `inspect_sources(...)` 都用 `repository`/`speech_repository` fixture 建**临时 git 仓库**，或传 `HEAD`/临时提交，不依赖 origin 分支。
- `scripts/dev-env/verify.sh:19` 用 `origin/main`，但先 `git rev-parse --verify origin/main` 守卫。
- 其余三张审批表测试（`test_approved_http_operation_removals.py`、`test_approved_http_parameter_and_response_changes.py`、`test_approved_http_request_bound_narrowings.py`）不做「对真实基线求值」的 liveness 断言，没有同类恒红；其中 `test_approved_http_request_bound_narrowings.py::test_the_real_speech_narrowing_is_the_only_filtered_change` 用的是临时仓库里构造冻结基线，正是本 PR 该用的形态。

结论：**本文件是唯一一处**把「PR 时性质」写成「main 上也成立」的测试；修好它就没有第二处。

## 六、例数

| 范围 | 结果 |
| --- | --- |
| `test_approved_http_response_vocabulary_changes.py`（main） | 1 failed, 4 passed |
| `test_approved_http_response_vocabulary_changes.py`（PR head） | 6 passed |
| `test_http_contract_changes.py`（PR head） | 152 passed |
| 上两文件合计 | 158 passed |
| `tests/contract` 全量（PR head） | 3873 passed, 8 failed |
| 同一作业在 CI（PR 事件） | 5964 passed, 2 failed |

本 worktree 的 8 条失败全为 `typescript` 参数化用例、`ERR_MODULE_NOT_FOUND: Cannot find package 'openapi-fetch'`（未装 `node_modules`），CI 装了 pnpm 依赖，同一作业里没有这 8 条。除这 8 条外，PR head 上 `tests/contract` 无失败。

## 七、改动面

```
$ git diff origin/main...9ed6b740 --stat
 ...test_approved_http_response_vocabulary_changes.py | 78 +++++++++++++++++++---
 1 file changed, 69 insertions(+), 9 deletions(-)
```

只动一个测试文件，未改产品代码、契约、生成物，符合边界。测试函数改名（`test_every_entry_matches_a_finding_of_the_real_comparison` → `test_every_entry_matches_a_widening_the_current_contract_still_carries`）没有 CI 侧按 nodeid 的引用；仓库内的旧名只出现在 `.pytest_cache` 与 `docs/validation/2026-11-15-engine-readiness-expiry.md:300` 的历史记录里，无需改。

## 必须修

1. **（阻断）让测试能在 CI 的实际检出形态下拿到基线。** 现写法只在完整克隆成立，PR 作业与本 PR 自己的 CI 都红；合入后 `push` 作业（无 base 抓取、`fetch-depth: 1`）同样红，`main` 不会变绿。两条可行路线：
   - **首选**：把审批前的 `contracts/speech-openapi.yaml` 冻结成 fixture，照 `tests/contract/test_approved_http_request_bound_narrowings.py::test_the_real_speech_narrowing_is_the_only_filtered_change` 的做法，在 `repository` fixture 的临时仓库里写入 fixture 字节、提交，再覆盖成当代契约后 `inspect_sources(<本地提交>)`。无网络、无历史依赖、PR 与 push 事件一致，且能本地复现。
   - **次选**：在测试里按 SHA 抓一次再读，模式照 `scripts/dev-env/check-review-pr-boundary.py`（`_ensure_commit` / `_fetch_commit`：`git fetch --no-tags --depth=1 origin <sha>`，失败退避）；同时把 CI 两个作业的 "Fetch only the compatibility baseline" 步骤扩展到也抓这个 pin（注意 `push` 事件该步骤本就不执行，仍需测试侧兜底）。`fetch-depth: 0` 只能拖延、不是修复。
   - 无论哪条，合并前用「浅克隆复现」证明：`git init` → `fetch --depth=1 origin main`（及 PR head）→ 跑该文件，必须全绿；这正是本次 CI 红掉的形态。
2. **（阻断）残留断言必须覆盖 `const value changed`。** 当前 22/27 条审批落在该类，未登记 const 放宽会静默通过（B2、W1）。最小改法：类别集合从审批表里出现的类别推导，或至少写成 `("enum values added", "const value changed")`；同时修正 docstring 中「表未命名的放宽也会红」的表述。
3. **（随修）第二条测试的 git 依赖与失败信息。** 与第 1 条同源；若沿用 fixture 路线，`git show` 应改为读 fixture 字节，避免同一形态再红一次。
4. **（非阻断，另开 issue）审批键绑定值。** 实测 H 显示同一指针换值后测试与门禁同时绿，`const`/`enum` 的「登记值」未被守住。内核 finding 文本带上新值（或表中登记期望值）才能根治；这超出本热修范围，但应在审批机制上留一条跟踪。

## 只能依赖作者陈述 / 未验证的点

- **未在真实 `push` 事件上验证**（不能合并、不能 push main）：该结论依据 `.github/workflows/ci.yml` 的 `if: github.event_name == 'pull_request'` 与 `fetch-depth: 1`，加上本地真实远端深度 1 抓取复现（第一节）。
- **未在 CI 的 Linux runner 上重跑全文**：只读了 PR #855 实际 run 的日志（`2 failed, 5964 passed`）；8 条 `typescript` 失败按本 worktree 环境（缺 `node_modules`）判定，未在装有 pnpm 依赖的环境复核。
- **未核对 PR 描述中「本 PR 的首个 run 即是验证」之后的修复意愿**：首个 run 已 fail，结论以该 run 日志为准。
- 冻结基线 pin 的**选点**（是否恰好是审批前一刻）本审查按作者陈述接受；我独立核到的是：该提交是 `origin/main` 祖先、其中契约不含 `voice_draft_engine_unavailable`、以它为基线时 27 条审批全部命中且残留 finding 为 0。

---

# 第二轮审查（head `1bfc3737`）

1bfc3737 的改动正是第一轮的两条建议：不再 pin SHA，改为提交 fixture并在临时仓库里做真实比对；残留断言按类别推导。下面逐项独立复核，变异只在 `/private/tmp/shallow855`（从 GitHub 真实 `--depth=1` 抓的克隆）里做，跑完复原，未改产品代码/契约，未合并、未 push main。

| 项 | 值 |
| --- | --- |
| 被审 head | `1bfc37377dd662e4379927d756106f6e9a9de408`（`fix/855-vocabulary-baseline`） |
| 基线 | `origin/main` = `c7e43f9a2926507578513029ee8d1ee07ebce688` |
| CI 运行 | run `43737601262`（PR 事件，head `1bfc3737`） |
| 复核时间 | 2026-11-16，Asia/Shanghai |

## 一、浅克隆：真的全绿（上一轮的死因已消除）

从 GitHub 真实抓一个 `--depth=1` 的克隆，并在克隆内用自己的 venv（`uv sync --offline`，不是复用 `/private/tmp/sn858/.venv`）：

```
$ git clone --depth=1 --branch fix/855-vocabulary-baseline https://github.com/hrygo/SpeechNexus.git /private/tmp/shallow855
Cloning into '/private/tmp/shallow855'...
$ cd /private/tmp/shallow855 && git rev-parse HEAD && git rev-list --count HEAD && cat .git/shallow
1bfc37377dd662e4379927d756106f6e9a9de408
1
1bfc37377dd662e4379927d756106f6e9a9de408
$ git rev-parse HEAD^
fatal: ambiguous argument 'HEAD^': unknown revision or path not in the working tree.
$ uv sync --offline            # 克隆内自建 venv
$ .venv/bin/python -m pytest tests/contract/test_approved_http_response_vocabulary_changes.py -q
......                                                                   [100%]
6 passed in 3.12s
```

确认测试解析到的是这份克隆而不是别处：

```
ROOT      = /private/tmp/shallow855
FIXTURE   = /private/tmp/shallow855/tests/contract/fixtures/speech-openapi.pre-828.yaml True
len table = 27
```

同一克隆里换回上一版（`9ed6b740`）的测试正文，复现出与第一轮 CI 同一形状的失败——这正是本次改动修掉的东西：

```
$ .venv/bin/python -m pytest tests/contract/test_approved_http_response_vocabulary_changes.py -q
E       subprocess.CalledProcessError: Command '['git', '-C', '/private/tmp/shallow855',
    'show', 'f3e9e1b4b5e5397fb2d4af7f59e06dc790614065:contracts/speech-openapi.yaml']'
    returned non-zero exit status 128.
fatal: path 'contracts/speech-openapi.yaml' exists on disk, but not in 'f3e9e1b4...'
FAILED ...::test_every_entry_matches_a_widening_the_current_contract_still_carries
FAILED ...::test_the_frozen_baseline_predates_the_widenings_it_is_compared_against
2 failed, 4 passed in 0.07s
```

CI 侧同向：run `43737601262` 全部通过，第一轮红掉的两个作业现在是 `pass`——`Python: tests, coverage and build`（6m3s）、`Python: lint, types and compatibility`（1m39s），下游 `Gate: Python tests and static checks`、`Gate: required checks for this change` 均 `pass`。**上一轮的阻断确实消除了。**

## 二、`const` 漏洞：补上了

| # | 变异（临时副本） | 期望 | 实测 |
| --- | --- | --- | --- |
| A | 删掉一条已登记的 `const value changed`（`pronunciation-sets/get/.../headers/x-should-retry`，契约仍带该放宽） | 红 | **红**，且由残留断言点名这一条 |
| B | 把该 503 的 `X-Should-Retry`、`retryable` 回退成 const（放宽消失） | 红 | **红**：`approval_without_widening` 两条 |

A：

```
E       AssertionError: ['contracts/speech-openapi.yaml:paths//v1/nexus/pronunciation-sets/get/responses/503/headers/x-should-retry: const value changed']
E       assert [...] == []
1 failed, 5 passed
```

B：

```
E       AssertionError: {'widening_not_approved': [], 'approval_without_widening':
  ['.../pronunciation-sets/get/responses/503/application/json/allOf/1/properties/error/properties/retryable: const value changed',
   '.../pronunciation-sets/get/responses/503/headers/x-should-retry: const value changed']}
1 failed, 5 passed
```

第一轮的 B2/W1（删 const 条目 / 新增未登记 const 改动仍绿）不再复现。

## 三、enum 方向仍红

| # | 变异 | 期望 | 实测 |
| --- | --- | --- | --- |
| C | 删掉一条已登记的 `enum values added`（voice-designs 503 码表） | 红 | **红**：`leftovers` 点名该 enum finding |
| D | 插一条指向不存在放宽的条目（`voice_draft_phantom_code`） | 红 | **红**：`approval_without_widening` |

## 四、fixture 真的早于审批，且不能被偷偷换掉

字节比对（`git show c7e43f9a^:contracts/speech-openapi.yaml` 对 fixture）：

```
historical blob sha256: 54283b9ec59ab44e0a06f72ef03050fcc53518ca00fc3c49b8049a49f4f2dfc6
fixture         sha256: 54283b9ec59ab44e0a06f72ef03050fcc53518ca00fc3c49b8049a49f4f2dfc6
byte-identical: True | sizes: 209338 209338
```

内容上：fixture 不含 `voice_draft_engine_unavailable`，当代契约含；`voice_draft_admission_contended` 同理。把 fixture 直接换成当代契约字节（模拟被悄悄换掉）：

```
E       AssertionError: the fixture already contains voice_draft_engine_unavailable, so it does not predate the approvals
FAILED ...::test_every_entry_matches_a_widening_the_current_contract_still_carries
FAILED ...::test_the_frozen_fixture_predates_the_widenings_it_is_compared_against
2 failed, 4 passed
```

两条都红：防空转的那条直说原因，集合相等那条因为对比对象等于工作树而失效。防线有效。

## 五、fixture 位置与体积：无副作用

作者判断「`tests/contract/fixtures/` 不在 web 镜像构建上下文、`tests/fixtures/contracts/` 才是」**成立**：

```
$ grep -n COPY deploy/images/web.Dockerfile
20:COPY package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json .npmrc ./
21:COPY web web
22:COPY packages/contracts packages/contracts
33:COPY deploy/web/nginx.conf /etc/nginx/templates/default.conf.template
34:COPY deploy/web/19-configure-upstreams.envsh /docker-entrypoint.d/19-configure-upstreams.envsh

$ python3 scripts/dev-env/verify-web-image-context.py
deploy/images/web.Dockerfile:
  @speechnexus/admin: 22 production files
  @speechnexus/workbench: 78 production files
  7 COPY sources, no test data, no ignored path
(exit 0)
```

`tests/` 整棵树都不在 COPY 集合里；`tests/fixtures/contracts/` 之所以敏感，是因为 `web/admin/src/profiles/profile-test-fixtures.ts`、`web/admin/src/task-center/dev/demo-transport.ts`、`web/workbench/src/manuscripts/*.test.ts` 等直接 import 它。新 fixture 放 `tests/contract/fixtures/` 不进任何一条构建图，`--build` 级的证据不受影响。

209KB／6043 行是否会被别的检查当成输入而变红：

| 检查 | 结果 |
| --- | --- |
| `ruff check .` / `ruff format --check .` | `All checks passed!` / `1912 files already formatted` |
| `mypy` | `Success: no issues found in 1484 source files` |
| `validate-contracts.py` | `contract validation: passed (37 files)`（只 glob `contracts/`） |
| `check-operation-coverage.py` | `{"operations": 52, "bound": 42, "pending": 10, "findings": []}` |
| `verify-web-image-context.py`（静态） | 通过，见上 |
| `tests/ci` 全量 | `414 passed, 1418 subtests passed` |
| `tests/unit/test_safe_yaml.py` 的 glob | 只扫 `contracts/`，fixture 不在其中 |
| `check-doc-refs.py` | 只扫 `docs/**/*.md`；该脚本未接入 CI，现状为 `SUMMARY ... MISSING=27 ... RESULT FAIL`，与本次改动无关 |
| `generate-contract-bindings.py --check` | 在本 worktree 失败，但原因是 `Cannot find package 'ajv'`（`packages/contracts/scripts/generate-standalone-validators.mjs`，未装 `node_modules`）；生成器输入根为 `CONTRACTS = ROOT / "contracts"`，fixture 不是输入 |
| PR 时门禁 `check-http-contract-changes.py --base origin/main --surface speech` | `Known HTTP structural checks: passed` |

体积与位置都不构成副作用。

## 六、`_finding_class` 解析：const 对，enum 错（必修）

现写法 `finding.rsplit(": ", 1)[-1].split(":", 1)[0]` 从**最后**一个 `": "` 切，对 `const value changed` 正确，对 `enum values added: [...]` 取到的是码表：

```
_finding_class(".../x-should-retry: const value changed")            -> 'const value changed'
_finding_class(".../code: enum values added: ['voice_draft_admission_contended', 'voice_draft_engine_unavailable']")
                                                                     -> "['voice_draft_admission_contended', 'voice_draft_engine_unavailable']"
_finding_class(".../code: enum values added: ['probe_novel']")        -> "['probe_novel']"

derived classes set has 2 members:
  - "['voice_draft_admission_contended', 'voice_draft_engine_unavailable']"
  - 'const value changed'
```

于是「按类别推导」得到的集合里根本没有 `enum values added`。第三节 C 之所以仍红，是因为残留那 5 条 enum 审批的码表字符串**恰好**与集合里那条相同，属偶然耦合，docstring 里「a new class cannot pass unguarded」对 enum 类不成立。

实物证明（F2）：在真实契约里给 `retirements` POST 503 的 `code` 枚举加一个未登记码，比对确实产出 finding，但该文件仍绿：

```
findings: 1
  - contracts/speech-openapi.yaml:paths//v1/nexus/voices/{voice_id}/revisions/{revision}/retirements/post/responses/503/application/json/allOf/1/properties/error/properties/code: enum values added: ['probe_unregistered_code']
approved vocabulary: 27

$ .venv/bin/python -m pytest tests/contract/test_approved_http_response_vocabulary_changes.py -q
......                                                                   [100%]
6 passed
```

即**未登记的 enum 放宽逃过守卫**（假阴性）。候选修法（已在沙箱验证：F2 变红，正常树仍绿）：

```
def _finding_class(finding: str) -> str:
    """The kind of change a finding names, e.g. `enum values added`."""
    tail = finding.split(": ", 1)[1]      # 改前：rsplit(": ", 1)[-1]
    return tail.split(":", 1)[0]
```

finding 的形态是 `<source>:<pointer>: <class>...`，source 与 pointer 都不含 `": "`，所以首个 `": "` 就是指针与类别的边界；建议再加一句 `if ": " not in finding: return ""` 兜底。

顺带一条实测：给 `voice-designs` 503 的码表加码会被 `approved == table` 抓到（因为已登记条目的 finding 文本变了）。所以 F2 这类「另一处、另一种码表」的未登记放宽才是当前真正的漏网形状。

## 七、新的必修：契约版本号一升就红

审批表的 `from_version`/`to_version` 被钉成字面量 `0.1.0.dev18`，fixture 的版本也是 `0.1.0.dev18`，`_apply_approved_response_vocabulary_widenings` 只在「比对报告的版本对」与条目一致时过滤。⇒ 只要当代契约的 `version` 变了，27 条全部不再过滤，`approved == table` 立刻失败，而词汇本身没有任何变化：

```
### MUTATION G：只把 contracts/speech-openapi.yaml 的 version: 0.1.0.dev18 改成 0.1.0.dev19
E       AssertionError: ... Extra items in the right set:
E         '.../voice-drafts/{draft_id}/publications/post/responses/503/.../retryable: const value changed'
E         ".../voice-clones/post/responses/503/.../code: enum values added: ['voice_draft_admission_contended', 'voice_draft_engine_unavailable']"
E         '.../pronunciation-sets/get/responses/503/headers/x-should-retry: const value changed'
E         ...（共 27 条）
FAILED ...::test_every_entry_matches_a_widening_the_current_contract_still_carries
1 failed, 5 passed
```

这不是假想：`contracts/speech-openapi.yaml` 的 `version:` 行在最近 12+ 个特性 PR 里逐一递增（`git log -L` 命中 #611、#610、#552、#546、#542、#525、#523、#518、#497…），即**下一个动契约的 PR 就会红 `tests/contract`**，且报错文案指名叫人删/改 #828 的审批，看不出真实原因。更难堪的是两条自然逃法都不对：删条目会让 27 条 finding 重新变成 `leftovers` 而红；重抓 fixture 则等于让 #828 的守卫失效、fixture 也就不再「冻结」。

候选修法（已在沙箱验证：G 变绿，同时第二节 A 仍红，即守卫未削弱）：在断言身份前把每条审批的版本对对齐到本次比对实际报告的版本对，版本门本身仍由 `test_a_registered_line_filters_only_its_own_source_and_version_pair` 守着。

```
def _align_registered_version_pair(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    old = module._version(yaml.safe_load(_PRE_APPROVAL_FIXTURE.read_text(encoding="utf-8")))
    new = module._version(yaml.safe_load((ROOT / SOURCE).read_text(encoding="utf-8")))
    aligned = {finding: {**entry, "from_version": old, "to_version": new}
               for finding, entry in module._APPROVED_RESPONSE_VOCABULARY_WIDENINGS.items()}
    monkeypatch.setattr(module, "_APPROVED_RESPONSE_VOCABULARY_WIDENINGS", aligned)
```

（在 `_real_contract_against_the_frozen_baseline` 返回后、`inspect_sources` 前调用。）若作者更愿意保留字面量版本，则必须把「版本 bumped 时同步改 27 条」写进 docstring 的维护说明，并让失败信息能直接说明这一点。

## 八、例数

| 范围 | 结果 |
| --- | --- |
| `test_approved_http_response_vocabulary_changes.py`（head，本 worktree） | 6 passed |
| 同上（`--depth=1` 克隆，自建 venv） | 6 passed |
| 同上（上一版正文，同一浅克隆） | 2 failed, 4 passed |
| `test_http_contract_changes.py` | 152 passed |
| `tests/contract` 全量 | 3873 passed, 8 failed |
| `tests/ci` 全量 | 414 passed, 1418 subtests |
| CI run `43737601262` | 全部 pass |

`tests/contract` 的 8 条失败全为 `typescript` 参数化（`ERR_MODULE_NOT_FOUND`，本 worktree 无 `node_modules`），在 `origin/main`（`c7e43f9a`，worktree `/private/tmp/sn856`）上同样复现 `test_workbench_streaming_tcp.py::test_native_tcp_download_sse_resume_expiry_and_cancel[typescript]`、`test_pronunciation_read_http.py::test_typescript_native_tcp_consumes_generated_read_client_without_automatic_retry`，与本次改动无关；CI 上该作业为 pass。

## 九、改动面

```
$ git diff --stat origin/main...1bfc3737
 .../contract/fixtures/speech-openapi.pre-828.yaml  | 6043 ++++++++++++++++++++
 ...st_approved_http_response_vocabulary_changes.py |  115 +-
 2 files changed, 6148 insertions(+), 10 deletions(-)
```

只有该测试文件与新增 fixture，无产品代码、契约、生成物变化。测试自身的 git 调用全部落在 `tmp_path` 的临时仓库（`init`/`add`/`commit`/`rev-parse`），不再对本仓库任何提交做 `git rev-parse`/`git show`。

## 必修（第二轮）

1. **（阻断）`_finding_class` 用首段切分。** 现写法让 `enum values added` 类残留无人检查（第六节 F2 实测假阴性）；改 `split(": ", 1)[1].split(":", 1)[0]` 后 F2 即红、正常树仍绿。docstring「a new class cannot pass unguarded」在修好这条之前对 enum 类不成立。
2. **（阻断）解除版本对耦合，或明确写死维护方式。** 契约 `version` 一升该文件就红 27 条（第七节 G），下一个动契约的 PR 会踩；候选补丁已在沙箱验证（G 绿、A 仍红）。
3. **（随修）补一条「未登记 enum 放宽（另一处、不同码表）」的负例。** 现测试只有「删条目」「死审批」两个方向，F2 这种形状没有覆盖；加上后即使将来 `_finding_class` 再漂移也会直接红。
4. **（非阻断，沿第一轮第 4 条）审批键仍不绑定值。** 同一指针换值后测试与门禁同时绿的问题未变，属内核 finding 文本粒度问题，另开 issue 跟踪。

## 只能依赖作者陈述 / 未验证的点（第二轮）

- **未在 CI 的 Linux runner 上手工重跑**：本轮 CI 证据只取 PR #855 最新 run `43737601262` 的作业状态（全部 pass），未拉日志逐字核对；本地证据是真实 `--depth=1` 克隆（第一节）。
- **`generate-contract-bindings.py --check` 未跑通**：本 worktree 缺 `node_modules`（`ajv` 缺失）；按输入根 `contracts/` 判定 fixture 不是其输入、且这是环境限制而非本 PR 引入，但未在装有 pnpm 依赖的环境复核该步骤。
- **版本号递增频率**取自 `git log -L '/version: 0.1.0.dev/,+1:contracts/speech-openapi.yaml'` 的历史命中（12+ 个特性 PR），未核对每一次递增是否都发生在契约 PR 内。
- 变异全部在 `/private/tmp/shallow855` 内进行并已复原（`git status` 干净、该文件 6 passed）；`/private/tmp/sn858` 只新增了本文档。未启动 PG、未合并、未 push main。

---

# 第三轮审查（head `76a7359a`）

| 项 | 值 |
| --- | --- |
| 被审 head | `76a7359a6ea30bf5a88fd7bc480103b994843055`（`fix/855-vocabulary-baseline`） |
| 基线 | `origin/main` = `c7e43f9a2926507578513029ee8d1ee07ebce688` |
| CI run | `43739451588`（PR 事件，head `76a7359a`）：23 个 job 无 failure（21 success、2 skipped：docs markdown formatting、node-controller image build），含 `Python: tests, coverage and build` 与三个 Gate；PR 现为 `MERGEABLE` / `CLEAN` |
| 审查 worktree | `/private/tmp/sn858`（detached 在 head 上跑；全部变异按字节备份复原，`sha256sum -c` 通过） |
| 浅克隆 | `/private/tmp/shallow855b`（从 GitHub 真实 `--depth=1` 抓同分支，克隆内自建 venv） |
| 复核时间 | 2026-11-16，Asia/Shanghai |

## 一、阻断 1 真解：`_finding_class` 改成首段切分

解析对三类真实串都稳（新写法 / 旧写法）：

```
new: 'const value changed'
old: 'const value changed'

new: 'enum values added'
old: "['voice_draft_admission_contended', 'voice_draft_engine_unavailable']"

new: 'const value changed'          # retirements POST 503 的 headers/x-should-retry（带 headers 的那类）
old: 'const value changed'

derived classes: {'enum values added', 'const value changed'}
has 'enum values added': True | has 'const value changed': True
```

finding 形态固定为 `<source>:<pointer>: <class>`，source 与 JSON pointer 都不含 `": "`，所以首个 `": "` 就是指针与类别的边界；enum 的类别自带 `": ["`，只有从右切才会取到码表。另核：27 条审批的 `reason` 里没有 `" ["`，`rsplit(" [", 1)` 剥离过滤器后缀仍是单值确定的（`identity == table: True`）。

真实契约实测——给**另一处** 503 码表（不是已登记的那 5 处，避免「已登记条目文本变了」顺带红）加一个未登记码：

```
MUTATION M2：contracts/speech-openapi.yaml 的 retirements POST 503 码表加 - voice_catalogue_probe_unregistered
$ .venv/bin/python -m pytest tests/contract/test_approved_http_response_vocabulary_changes.py -q
E  AssertionError: {'widening_not_approved': ["contracts/speech-openapi.yaml:paths//v1/nexus/voices/{voice_id}/revisions/{revision}/retirements/post/responses/503/application/json/allOf/1/properties/error/properties/code: enum values added: ['voice_catalogue_probe_unregistered']"], 'approval_without_widening': []}
FAILED ...::test_every_entry_matches_a_widening_the_current_contract_still_carries
1 failed, 5 passed in 2.12s
```

红的就是这一条 finding、由 `widening_not_approved` 点名，且只有那条测试失败（其余 5 条不受影响），不是靠别的断言顺带红。同一棵树上 PR 时门禁自己也拒绝：

```
$ .venv/bin/python scripts/dev-env/check-http-contract-changes.py --base origin/main --surface speech
  - .../retirements/post/responses/503/.../code: enum values added: ['voice_catalogue_probe_unregistered']
Known HTTP structural checks: rejected
```

## 二、阻断 2 真解：只 bump `info.version` 不再红

```
MUTATION M1：contracts/speech-openapi.yaml 只把 info.version 从 0.1.0.dev18 改成 0.1.0.dev19
$ .venv/bin/python -m pytest tests/contract/test_approved_http_response_vocabulary_changes.py -q
......                                                                   [100%]
6 passed in 2.00s
```

绿的不是空转——新断言改用原始 finding，而同一次 fixture 比对里过滤器一条也没放行：

```
rows versions: [('0.1.0.dev18', '0.1.0.dev19')]
raw findings: 27  approved_filtered: 0
carried: 27 table: 27
```

同时确认这个 bump 在 PR 时门禁上本来就不红（门禁基线是 `origin/main`，已含 #828 全部放宽，这 27 条 finding 根本不产生）：

```
$ .venv/bin/python scripts/dev-env/check-http-contract-changes.py --base origin/main --surface speech
contracts/speech-openapi.yaml: 0.1.0.dev18 -> 0.1.0.dev19
Known HTTP structural checks: passed        (gate exit: 0)
```

即第二轮查出的「版本一升就红 27 条」确实只发生在测试侧，作者的诊断成立；修法也没削弱门禁（门禁脚本未改动）。

## 三、前两轮已验性质：全部仍红

全部变异在 head 上做，跑完按字节复原（`sha256sum -c` 通过、`git status` 干净）。

| # | 变异 | 期望 | 实测 |
| --- | --- | --- | --- |
| 0 | 正常树 | 绿 | `6 passed` |
| A | 插一条死审批（指向 `voice_draft_never_existed`） | 红 | 红，`approval_without_widening` 点名 |
| B1 | 删一条已登记 enum 条目 | 红 | 红，`widening_not_approved` 点名 |
| B2 | 删一条已登记 const 条目 | 红 | 红，`widening_not_approved` 点名 |
| C | 回退已登记 const（`X-Should-Retry` 改回 `const: "true"`） | 红 | 红，`approval_without_widening` 点名 |
| D | 未登记 const 改动（`Cache-Control` `no-store` → `no-cache`） | 红 | 红，`widening_not_approved` 点名 |
| E | fixture 换成当代契约 | 红 | 两条都红（防空转 + 集合相等） |
| M2 | 未登记 enum 码（另一处码表） | 红 | 红，见第一节 |

关键输出（节选）：

```
A   Extra items in the right set:
    ".../voice-designs/post/responses/503/.../code: enum values added: ['voice_draft_never_existed']"
    1 failed, 5 passed

B1  Extra items in the left set:
    ".../voice-designs/post/responses/503/.../code: enum values added: ['voice_draft_admission_contended', 'voice_draft_engine_unavailable']"
    1 failed, 5 passed

B2  Extra items in the left set:
    '.../pronunciation-sets/get/responses/503/headers/x-should-retry: const value changed'
    1 failed, 5 passed

C   Extra items in the right set:
    '.../pronunciation-sets/get/responses/503/headers/x-should-retry: const value changed'
    1 failed, 5 passed

D   AssertionError: {'widening_not_approved': ['.../retirements/post/responses/503/headers/cache-control: const value changed'], 'approval_without_widening': []}
    1 failed, 5 passed

E   FAILED ...::test_every_entry_matches_a_widening_the_current_contract_still_carries
    FAILED ...::test_the_frozen_fixture_predates_the_widenings_it_is_compared_against
    2 failed, 4 passed
```

第一轮的 B2/W1（「删 const 条目仍绿」）在 `1bfc3737` 之后没再复现，本轮改动也没把它带回来。

## 四、`carried == table` 的宽窄，以及版本对取舍

### 4.1 太宽？

**源侧足够。** 这条测试用 `inspect_sources(baseline, surface="speech")`，而 `surface="speech"` 只选 `(SPEECH_SOURCE,)`，findings 只会来自 `contracts/speech-openapi.yaml`；`startswith(SOURCE)` 冗余但正确，且 `SOURCE` 带 `.yaml` 后缀，不会被同前缀的其它源命中。

**方向侧过宽。** 过滤是「类别 + 源」，没有限定 `/responses/`（27 条审批键却全在 `/responses/` 下）。构造请求侧同类 finding：

```
MUTATION W：给请求体组件 #/components/schemas/SpeechRequest 的 model 加 const: "tts-1"
E  Extra items in the left set:
   'contracts/speech-openapi.yaml:paths//v1/audio/speech/post/requestBody/application/json/properties/model: const value changed'
1 failed, 5 passed
```

这不是响应词汇放宽，却被并进 `carried` 并以 `widening_not_approved` 报出，与测试 docstring「同一文件的无关后续改动是 PR 门禁的事，不是这条测试的事」不一致。门禁对同类请求侧改动同样拒绝（实测 `rejected`），所以不存在漏放，只是报错语义错位、把作者引向错误的审批表。属随修：过滤加 `"/responses/" in finding`（或复用审批键的指针前缀），或把 docstring 改成与实现一致。

### 4.2 太窄？

核心里**值词汇类** finding 只有两种：`const value changed`（方向无关）与 `enum values added`（响应方向；请求方向发的是 `enum values removed`）。其余消息全是结构类：`path/operation/parameter/schema removed`、`type changed`、`format changed`、`required`/`response required guarantees removed`、`additionalProperties`/`boolean schema acceptance/guarantee changed`、`$ref changed`、`schema node kind changed`、`response status/header/media type removed`、`event types removed` 等。审批表两类的并集正好等于这两种，`classes` 又从表推导，所以「表里将来出现第三类会自动被断言」成立；表里没有的第三类词汇放宽不会被这条测试发现，但它同样不可能被静默放行——门禁对任何未登记 finding 都会拒绝（第一节 M2 已证）。判断：不窄。

### 4.3 「版本对错了但词汇对」不再红：可接受，但不能说「版本对仍有守护」

版本对在 main 侧的唯一消费者就是这条断言刚绕开的过滤器（`_apply_approved_response_vocabulary_widenings` 的 `(from_version, to_version)` 匹配）。PR 时门禁的基线是 `origin/main`（已含 #828 的放宽），那 27 条 finding 根本不会产生，因此版本对记错在 CI 上没有任何可见后果。

实测（只动审批表的记录、不动词汇）：

```
V1   把最后一条（非首条）记录的版本对改成 0.1.0.dev17 → 该文件 6 passed；门禁 --base origin/main 也 passed
V1b  把除首条外的 26 条改成 0.1.0.dev17             → 该文件 6 passed
V2   把 27 条全改成 0.1.0.dev17                     → 2 failed（test_a_registered_line_filters_only_its_own_source_and_version_pair、
                                                         test_an_entry_without_its_reason_is_reported_instead_of_filtered）
```

V2 的红不是「版本对断言」，而是两条合成用例把 `next(iter(table))`（首条）硬编码成 `0.1.0.dev18` 造成的副作用——**只有首条被偶然守住，其余 26 条没有任何守护**；`test_the_table_is_not_empty_and_every_entry_is_fully_annotated` 只检查 `from_version`/`to_version` 非空。因此第二轮必修 2 的解是「解耦」而非「搬走约束」，取舍成立：该字段的语义只剩「注册当下的比对版本对」，而 fixture 冻结在 dev18、当代契约版本会继续上涨，历史 27 条的 `to_version` 事实上不可能再与真实比对版本对相等；把它重新接回断言（我第二轮给的 align 补丁就是这种思路）只是换一种方式再红一次。

替代建议（都不阻断）：

1. 冻结类别集合 `assert classes == {"const value changed", "enum values added"}`——这是第二轮随修 3 的最省事替代：`_finding_class` 再漂移时直接红，而不是靠「另一处码表 + 人工构造」才发现假阴性。
2. 在注册表注释里写明版本对只对注册当下的比对有意义（`docs/validation/http-contract-change-checks.md` 现有描述「entry 只在其精确版本对下生效」仍然准确，无需改）。

## 五、浅克隆仍全绿（第二轮的构造性证明未失效）

从 GitHub 真实 `--depth=1` 抓同一分支，克隆内自建 venv：

```
$ git clone --depth=1 --branch fix/855-vocabulary-baseline https://github.com/hrygo/SpeechNexus.git /private/tmp/shallow855b
$ cd /private/tmp/shallow855b && git rev-parse HEAD && git rev-list --count HEAD && cat .git/shallow
76a7359a6ea30bf5a88fd7bc480103b994843055
1
76a7359a6ea30bf5a88fd7bc480103b994843055
$ git rev-parse HEAD^
fatal: ambiguous argument 'HEAD^': unknown revision or path not in the working tree.

ROOT   : /private/tmp/shallow855b
FIXTURE: /private/tmp/shallow855b/tests/contract/fixtures/speech-openapi.pre-828.yaml True
table  : 27

$ .venv/bin/python -m pytest tests/contract/test_approved_http_response_vocabulary_changes.py -q
......                                                                   [100%]
6 passed in 2.81s
```

同一浅克隆里把两处修各验一次：只 bump `version` → `6 passed`；加未登记 enum 码 → `1 failed, 5 passed` 且报同一条 finding。测试的 git 调用仍只落在 `tmp_path` 的临时仓库（`init`/`add`/`commit`/`rev-parse`），不读本仓库历史，第二轮的修法没有因本轮改动回退。

## 六、例数

| 范围 | 结果 |
| --- | --- |
| `test_approved_http_response_vocabulary_changes.py`（head，正常树，跑三次一致） | `6 passed` |
| 同上（`--depth=1` 克隆，自建 venv） | `6 passed` |
| `test_http_contract_changes.py` | `152 passed` |
| `tests/contract` 全量（head） | `3873 passed, 8 failed` |
| CI run `43739451588`（PR 事件） | 23 个 job 无 failure（21 success、2 skipped），含 `Python: tests, coverage and build`、`Gate: Python tests and static checks`、`Gate: integration partitions and merged coverage`、`Gate: required checks for this change` |

8 条失败与第二轮同一形状：全为 `typescript` 参数化用例，`ERR_MODULE_NOT_FOUND`（本 worktree 未装 `node_modules`），CI 装了 pnpm 依赖、同一作业为 pass：

```
FAILED test_binding_defaults_producer_contract.py::...[True-typescript]
FAILED test_binding_defaults_producer_contract.py::...[False-typescript]
FAILED test_profile_lifecycle_http_bindings.py::...[True-publish-typescript]
FAILED test_profile_lifecycle_http_bindings.py::...[True-activate-typescript]
FAILED test_profile_lifecycle_http_bindings.py::...[False-publish-typescript]
FAILED test_profile_lifecycle_http_bindings.py::...[False-activate-typescript]
FAILED test_pronunciation_read_http.py::test_typescript_native_tcp_consumes_generated_read_client_without_automatic_retry
FAILED test_workbench_streaming_tcp.py::test_native_tcp_download_sse_resume_expiry_and_cancel[typescript]
```

## 七、改动面

```
$ git diff --stat origin/main...76a7359a
 .../contract/fixtures/speech-openapi.pre-828.yaml  | 6043 ++++++++++++++++++++
 ...st_approved_http_response_vocabulary_changes.py |  124 +-
 2 files changed, 6157 insertions(+), 10 deletions(-)
```

只有该测试文件与新增 fixture；未改产品代码、契约、生成物，`scripts/dev-env/check-http-contract-changes.py`（审批表本体）未动，符合边界。本轮 head 相对 `1bfc3737` 只有该测试文件 `+23/−14`。

## 八、必须修与建议

**必须修：无。**

建议（均不阻断，可随后续 PR 处理）：

1. 残留集合加 `/responses/` 限定，或改 docstring 使其与实现一致（4.1 的 W 形态）。
2. 冻结类别集合断言 `classes == {"const value changed", "enum values added"}`，替代第二轮随修 3（4.3 第 1 点）。
3. 在注册表注释中写明版本对只对注册当下的比对有意义（4.3 第 2 点）。
4. 沿第一/二轮的「审批键不绑定值」问题（同一指针换值后测试与门禁同时绿）：本轮未复验，仍属内核 finding 文本粒度问题，另开 issue 跟踪，不计入本 PR。

## 只能依赖作者陈述 / 未验证的点（第三轮）

- **未在 CI 的 Linux runner 上手工重跑**：CI 证据取自 `gh run view 43739451588` 的 job 结论（全部 success），未拉日志逐字核对；本地证据是真实 `--depth=1` 克隆（第五节）。
- **未在装有 pnpm 依赖的环境复核** `tests/contract` 里那 8 条 `typescript` 用例；按第二轮同一环境的判定，属 `node_modules` 缺失而非本改动引入。
- **未复验 GPU / 集成作业**：本轮只跑 Python 契约测试；`Integration*` 与 `Gate: integration partitions and merged coverage` 的绿按 CI 结论采信。
- 变异全部在 `/private/tmp/sn858`（detached head `76a7359a`）与 `/private/tmp/shallow855b` 内进行，均按字节备份复原并 `sha256sum -c` 校验；`/private/tmp/sn858` 上只新增本文档。未合并、未 push main、未改产品代码/测试/契约。
