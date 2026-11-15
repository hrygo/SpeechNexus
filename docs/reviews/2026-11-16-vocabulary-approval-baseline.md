# 词汇放宽审批基线热修的非作者审查（PR #855）

## 结论

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
