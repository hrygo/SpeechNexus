# 工作台同一 Job 同一 Artifact 同版证据的非作者审查（PR #952，关联 #829）

**第三轮复核（head `5fbea085`）：可合（第三轮）。** A/B/C 三个边界均已闭合：`make_bucket` 建桶后报错会按已知桶名清桶；`finally` 每步独立清理、原始异常加 note、成功体清理失败抛点名步骤的 `RuntimeError`；`_serve` 超时取消任务/join/关 socket，3 次挂起 app 均只剩 `MainThread` 且端口可复绑。C 的 uvicorn `CancelledError` ERROR 栈只在启动超时路径出现，不改变 pytest/CI 结果。第二轮「未闭合」保留为历史。

**第二轮复核（历史，head `5b9a19bb`）：当时未闭合，第三轮已闭合。** 第 1、3 项闭合；第 2 项仍有三个边界（`make_bucket` 建桶后客户端报错、清理自身失败掩盖原始错误、`_serve` 启动超时线程未停），见文末「第二轮复核」。第一轮结论与证据保留如下。

**第一轮（head `d1885d0e`）：不能合。** 核心判定本身是硬的：真实 Chromium 对真实 PostgreSQL、真实 MinIO、真实 API 组合根与
`web/workbench/dist` 执行「恢复已成功 Job → 下载该 Job 的 transcript Artifact」，五项事实都有取值
断言；浏览器落盘字节的 SHA-256 由 `node:crypto` 对下载文件重算，我用「对象存另一份字节、提交行仍报
原摘要」的变异验证它确实会红；三条反例（另一 Job 的制品、篡改 `sha256`、两侧摘要不等）都能红。
阻断的是三处证据边界，不是产品缺陷：MinIO 缺失时 `pytest.skip`（退出码 0）、setup 失败泄漏
schema/角色/桶、以及 PR 正文声称的 console error 为空在测试里没有任何断言。

审查对象：`test/workbench-same-job-artifact`，第一轮 head `d1885d0e`（父 `94a76674`），第二轮 head `5b9a19bb`（父 `d1885d0e`），第三轮 head `5fbea085`（父 `5b9a19bb`）。审查分支
`review/952-same-job-artifact` 从 `origin/main`（`5ffb4eb7`）新建，本报告是该分支唯一改动。被审
工作树 `/tmp/sn952-subject`（第一轮 detached `d1885d0e`，第二轮 detached `5b9a19bb`，第三轮 detached `5fbea085`），审查工作树 `/tmp/sn952-review`。我不是作者，
没有改被审代码；所有变异都在被审工作树内做并还原，收尾时两个工作树 `git status --porcelain` 为空。

**行号口径：** 第一轮 §0–§9 的 `file:line` 按 head `d1885d0e` 计；「第二轮复核」按 head `5b9a19bb` 计；「第三轮复核」按 head `5fbea085` 计，逐条另标。环境：macOS 26 arm64，Python 3.14.9
（`UV_NO_CONFIG=1 uv sync --locked --all-packages --all-groups`），Node 26.12.1，pnpm 12.14.0，
Playwright `@playwright/test` 1.64.1 + `chromium-1248`。隔离服务为我自己起的
`sn952-review-pg`（PostgreSQL 16-alpine，`127.0.0.1:55482`）与 `sn952-review-minio`
（`speechnexus-minio:RELEASE.2025-09-07T16-13-09Z`，`127.0.0.1:59002`，独立桶），未触碰其它审查者
的 `sn892-pg` 或 `speechnexus-coordination-mysql`。所有 `uv`/`pytest` 命令都带 `UV_NO_CONFIG=1`。

---

## 0. diff 口径

`git merge-base origin/main d1885d0e` = `94a76674`；`origin/main` 顶端已前进到 `5ffb4eb7`（`#950`
的发布说明改写，只动 `docs/releases/mvp-0.1.0.md`，不涉及本 PR 的 3 个文件）。因此：

| 口径 | 结果 |
| --- | --- |
| `git diff --stat 94a76674 d1885d0e`（两点，merge-base） | 3 文件 +956 |
| `git diff --stat origin/main...d1885d0e`（三点） | 3 文件 +956（相同） |
| `git diff --stat origin/main d1885d0e`（裸两点） | 4 文件 +962/−7（把 `#950` 的反向差异也算进来，**不采用**） |

改动集（`git diff --numstat 94a76674 d1885d0e`）：

| 文件 | +/− |
| --- | --- |
| `docs/operations/same-version-check.md` | +1 / −0 |
| `tests/integration/test_workbench_artifact_browser.py`（新） | +762 / −0 |
| `tests/integration/workbench-artifact-probe.mjs`（新） | +193 / −0 |

`git diff 94a76674 d1885d0e -- docs/operations/same-version-check.md` 只追加一行（第 117 行），
没有改动既有判据。PR 为 `MERGEABLE`，CI 全部检查通过（见 §5）。

---

## 1. 核心判定能立住的部分（先说不构成假绿的地方）

### 1.1 真实组件不是名义上的

| 声称 | 实际接线 | 判据 |
| --- | --- | --- |
| 真实 Chromium | `workbench-artifact-probe.mjs` 用 `chromium.launch({headless:true})`，页面下载后 `readFile(download.path())`，`createHash("sha256")` 对落盘字节重算 | `tests/integration/workbench-artifact-probe.mjs:63`、`:161-178` |
| 真实 PostgreSQL | Alembic `upgrade head` 建唯一 schema，两个登录角色，`public_job_grants`/`artifact_reader_grants` | `tests/integration/test_workbench_artifact_browser.py:468-492` |
| 真实 MinIO | `Minio.make_bucket` + `VersioningConfig(ENABLED)` + `put_object`，读回经真实 `/content` 流 | `tests/integration/test_workbench_artifact_browser.py:489-492`、`:323-333` |
| 真实 API 组合根 | `create_app(...)`；`PostgresPublicJobs` 用真实只读池；Artifact 路由由 `ArtifactRuntime` 从 reader DSN + 对象存储构造，不是 `TestClient` 旁的替身 | `tests/integration/test_workbench_artifact_browser.py:500-516`；`backend/src/speechnexus_backend/app.py:420-430` |
| 真实构建产物 | `web/workbench/dist` 的 `index.html`/`assets`，不是内联 HTML | `tests/integration/test_workbench_artifact_browser.py:176-182` |
| 部署镜像层 | 设 `WORKBENCH_PAGE_IMAGE` 时 `docker run` 真实 `nginxinc/nginx-unprivileged` 镜像，经镜像 nginx、CSP、`/v1/` 代理读同一个 API | `tests/integration/test_workbench_artifact_browser.py:459-555`、`:241-333` |

### 1.2 五项事实逐项有取值，不是「元素存在」

`_assert_page_read_the_same_artifact`（`tests/integration/test_workbench_artifact_browser.py:582-618`）
对页面侧、API 侧、提交行/对象三处逐项断言：

| # | 事实 | 页面侧（浏览器实际拿到） | API 侧 | 提交行/对象 | 断言 |
| --- | --- | --- | --- | --- | --- |
| 1 | Job | `job-page-1` 出现在身份行，状态 `succeeded` | `GET /v1/nexus/jobs/job-page-1` 200，`id`/`status` 同值 | `speech_jobs.state='succeeded'` | `:610-615`、`:644-650` |
| 2 | Artifact | 按钮列表**恰好** `["下载原始制品 artifact-page-transcript-1"]` | `GET /v1/nexus/artifacts/{id}` 200，`kind=transcript`、`status=ready` | `speech_artifacts` 行同值 | `:613`、`:628-634`、`:646` |
| 3 | 字节数 | 下载文件 65 字节 | `/content` `content-length: 65` | 对象 `len=65`、Job 制品 `bytes=65` | `:587`、`:597`、`:648` |
| 4 | SHA-256 | 重算 `sha256:63560121…077f` | `/content` `etag` 同值，metadata `sha256` 同值 | 行 `sha256` 同值，Job 制品 `sha256` 同值 | `:586`、`:596`、`:631`、`:639`、`:647` |
| 5 | 契约摘要 | 页面 `/v1/nexus/version` 响应体 `contract_digest`，且无 `role=alert` | `GET /v1/nexus/version` 200 同值 | bundle 内含 `CONTRACT_DIGEST` | `:658`、`:660-667` |

页面侧不是只查「按钮存在」：它把按钮可见名、Job 身份行、下载字节数、重算摘要、`/content` 的
`etag`/`content-length`、`/version` 响应体、`role=alert`、`pageerror`、越界请求都取了值再比。

### 1.3 浏览器摘要确实是重算的，不是复用服务端值

我把 `_seed_committed_job` 写入对象存储的字节换成 `OTHER_TRANSCRIPT`，但提交行的 `sha256`/`bytes`
仍保持 `ours`（65 字节，长度不变，只有摘要变）。如果测试复用服务端 etag，就会绿；实测红在浏览器
摘要：

```
$ pytest -m integration "tests/integration/test_workbench_artifact_browser.py::test_the_page_reads_the_same_jobs_artifact" -q
E       AssertionError: {'suggestedFilename': 'transcript.json', 'bytes': 65,
  'sha256': 'sha256:0c6f9471…dcfb3a', 'isValidJson': True}
E       assert 'sha256:0c6f9…dcfb3a' == 'sha256:63560…650077f'
tests/integration/test_workbench_artifact_browser.py:586: AssertionError
```

服务端 `content-length` 仍是 65、`etag` 仍是原摘要，只有浏览器落盘字节的摘要变——这说明
`download["sha256"]` 是 `workbench-artifact-probe.mjs:161-168` 对 `download.path()` 的独立重算，
不是从 `responses` 里复制的。

### 1.4 bundle 摘要没有被夹具假摘要骗过

`web/workbench/dist/assets/index-npRtzbgA.js` 里同时存在：

```
sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
sha256:ad581b4691e104fea9fad79d28fe3b0a5b08faace31156bdb7b2fe7d5c9c7863
```

前者是 `contracts/workbench-http.schema.json` 里的导出示例（`export_demo`），后者才是生成的
`CONTRACT_DIGEST`。测试断言的是 `CONTRACT_DIGEST in bundle`
（`tests/integration/test_workbench_artifact_browser.py:658`），不是「bundle 里存在任意
`sha256:` 串」，因此假摘要不能满足它。`scripts/dev-env/check-image-contract-digest.py` 也显式忽略
该占位值：

```
$ UV_NO_CONFIG=1 uv run python scripts/dev-env/check-image-contract-digest.py \
    speechnexus/web-workbench:acf633ff9e71f573dbfef5082d0680e2a3aefa41
ignored fixtures sha256:aaaa… (committed placeholders, not this digest)
baked digest     sha256:ad581b46…7863 (index-npRtzbgA.js)
expected         sha256:ad581b46…7863
OK: the image's bundle carries this contract revision.
```

### 1.5 镜像层确实复用了同一判定

我在本机跑了可选镜像层，`CHECK` 行与 5 项判定都过：

```
$ WORKBENCH_PAGE_IMAGE=speechnexus/web-workbench:acf633ff… \
    pytest -m integration "…::test_the_page_reads_the_same_jobs_artifact" -q -s
CHECK image tier image=speechnexus/web-workbench:acf633ff… artifact=artifact-page-transcript-1 \
  sha256=sha256:63560121…077f
1 passed in 5.40s
```

镜像发布的 24 个应用文件与本机 `dist` 逐文件字节相同（我按路径+内容摘要树比较，`diff` 为空；镜像另
带 nginx 的 `50x.html`，所以「全目录树」摘要会不同，PR 正文的 `24 个文件` 口径指应用文件）。镜像里
`check-image-contract-digest.py` 读出 `sha256:ad581b46…`，与本机重算一致。

### 1.6 「与所请求 Job 关联」不是碰巧

生产读路径本身就是强绑定的：`PublicJobs._artifacts`（`backend/src/speechnexus_backend/speech/application/public_jobs.py:1271-1310`）
从 `result_json.transcript_artifact_id` 取候选，再校验 `artifact_id`、`kind`、`receipt_id` 三项；
`PostgresPublicJobs.get` 只按 `job_id` 读行。页面拿到的 `job.artifacts` 只能来自这个 Job 的
`result_json`，不是「同 workspace 里某个 Artifact」。测试再断言页面按钮列表**恰好**一项
（`tests/integration/test_workbench_artifact_browser.py:613`），API 的 `body["artifacts"]` **恰好**
一项（`:646`），所以「多给一个制品」「换成另一 Job 的制品」都会红。我也用真实读另一 Job 的变异确认
了这一点（§4.1）。

---

## 2. 阻断项

### B1. MinIO 缺失时是 `skip`，不是失败；直接 `pytest` 退出码 0

`_required_environment` 对 PostgreSQL 是 `pytest.fail`，对 MinIO 是 `pytest.skip`：

- `tests/integration/test_workbench_artifact_browser.py:163`（DB 缺失）：`pytest.fail(... "skip is not evidence")`
- `tests/integration/test_workbench_artifact_browser.py:168`（MinIO 缺失）：`pytest.skip(...)`

模块 docstring 还把它写成承诺：「Missing is an explicit skip」
（`tests/integration/test_workbench_artifact_browser.py:26`）。实测：

```
$ env -u SPEECHNEXUS_TEST_MINIO_ENDPOINT -u SPEECHNEXUS_TEST_MINIO_ACCESS_KEY \
      -u SPEECHNEXUS_TEST_MINIO_SECRET_KEY \
      SPEECHNEXUS_TEST_DATABASE_URL=… \
      UV_NO_CONFIG=1 uv run pytest -m integration \
      "tests/integration/test_workbench_artifact_browser.py::test_the_page_is_served_under_the_deployed_response_headers" -q -rs
s                                                                        [100%]
SKIPPED [1] tests/integration/test_workbench_artifact_browser.py:753: Configure an isolated MinIO in
SPEECHNEXUS_TEST_MINIO_ENDPOINT, SPEECHNEXUS_TEST_MINIO_ACCESS_KEY, SPEECHNEXUS_TEST_MINIO_SECRET_KEY;
skip is not evidence
1 skipped in 0.45s          # exit code 0
```

这与本目录的既有约定不一致：`tests/integration/README.md:138` 写明「When database DSNs are
configured, missing object-storage inputs fail rather than skip」；`tests/integration/README.md:23-24`
写「Both database DSNs and the isolated MinIO variables below are required for the complete integration
file; skipped cases are not evidence」。CI 的 `integration-shards.py check` 会把 skip 判成不合格
（`scripts/dev-env/integration-shards.py` 的 `verify_reports` 对 `skipped` 抛「A shard failed or
skipped a required integration case」），所以 **CI 分片门禁** 不会因此变绿；但直接 `pytest`
（PR 正文「运行方式」给的就是这条）会以 0 退出，让只看退出码的人把「没跑」当成「通过」。这不是
「skip 等于 pass」的语义问题，而是 **本地证据入口的失败语义与同文件 DB 分支不一致**。

**修法：** 把 `:168` 的 `pytest.skip` 改成 `pytest.fail`，错误信息点名三个变量；docstring
`:26` 同步改成「Missing is a failure」。这与同文件 `:163`、`:179`、`:192` 三处「缺前置即失败」
的风格一致，也让 PR 正文「缺 DSN 时用例失败并点名变量（不跳过）」的表述覆盖全部前置。

### B2. setup 失败会泄漏 schema、两个角色和桶

`_stack` 在 `try` 之前完成全部资源创建：`_migrate`、`CREATE ROLE` ×2、`make_bucket`、
`set_bucket_versioning`、两次 `_seed_committed_job`、`speech_pool`、`create_app`、`_serve`，
直到 `tests/integration/test_workbench_artifact_browser.py:537` 才进入 `try`，清理在 `:539-554`。
任何一步在 `:537` 之前抛错，`finally` 都不会执行。实测（不改代码，只把 MinIO secret 设错）：

```
$ SPEECHNEXUS_TEST_MINIO_SECRET_KEY=wrong_secret_for_leak_probe … \
    pytest -m integration "…::test_the_page_is_served_under_the_deployed_response_headers" -q
E       minio.error.S3Error: S3 operation failed; code: SignatureDoesNotMatch, …
E       bucket_name: sn-page-f07a41aeb76e
1 error in 0.83s

$ psql … -Atc "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'sn_page_int_%';
                 SELECT rolname FROM pg_roles WHERE rolname LIKE 'sn_page_%';"
sn_page_int_f07a41aeb76e
sn_page_artifact_f07a41aeb76e
sn_page_jobs_f07a41aeb76e
```

同一个泄漏在我第一次尝试「把 job-page-1 的 result_json 改指另一 Job」时也出现过：那条 SQL 被
`speech_jobs_status_guard()` 拒绝（`CheckViolation: public Job status_version must advance by one`），
失败点在 `:537` 之前，留下了 `sn_page_int_40d486e0a516`、两个角色和 `sn-page-40d486e0a516` 桶。
两处我都已手工清理（`DROP SCHEMA … CASCADE`、`DROP ROLE`、删桶），收尾时 `sn_page_int_%` / `sn_page_%`
计数为 0、`sn-page-*` 桶为空。

这违反 `tests/integration/AGENTS.md:28`：「在 `try` 中创建 schema，在 `finally` 中执行
`DROP SCHEMA IF EXISTS "<schema>" CASCADE`……创建失败、断言失败和并发分片都必须走这条清理路径。」
新用例的断言失败路径是走的（§4 的变异都在 `try` 内失败，清理正常），但 **创建失败路径** 不走。

**修法：** 把 `_migrate` 之后的全部创建放进 `try`，用 `contextlib.ExitStack` 或在 `try` 前把
`admin/pool/objects/container/front_server` 初始化为 `None`，在 `finally` 里按「已创建才清理」
处理；`_migrate` 本身也要在 `finally` 里 `DROP SCHEMA IF EXISTS`（它创建了 schema，但清理句在
后面）。这是结构性改动，不只是加一行。

### B3. `consoleErrors` 被采集但从未断言；PR 正文却声称「console error 为空」

probe 采集了 `consoleErrors`（`tests/integration/workbench-artifact-probe.mjs:76-78`、`:129`），
但 `_assert_page_read_the_same_artifact`（`tests/integration/test_workbench_artifact_browser.py:616-618`）
只断言 `alerts`、`pageErrors`、`outbound`，没有 `consoleErrors`。整份测试里唯一碰它的是
`tests/integration/test_workbench_artifact_browser.py:760`：

```python
assert not any("unsafe-eval" in text for text in evidence["consoleErrors"]), evidence["consoleErrors"]
```

它只排除 `unsafe-eval` 一种 CSP 文案，其他 `console.error` 一律放行。实测在 probe 里注入一条
`console.error("sn952 injected console error")`，主用例仍然绿：

```
$ pytest -m integration "…::test_the_page_reads_the_same_jobs_artifact" -q
1 passed in 2.65s
$ python -c '… json.load(open("…/same-job.json"))["consoleErrors"] …'
consoleErrors: ['sn952 injected console error']
pageErrors: []
```

PR 正文写的是「`pageerror`/console error/越界请求均为空」，而测试没有断言 console error；这不是
「断言语义偏弱」，是 **证据陈述比测试实际证明的多了一项**。镜像层也复用同一个 `_assert`
（`tests/integration/test_workbench_artifact_browser.py:682`），所以镜像层的 console error 同样
没有被断言。

**修法：** 在 `_assert_page_read_the_same_artifact` 里加
`assert evidence["consoleErrors"] == [], evidence["consoleErrors"]`；若确有已知可接受的浏览器
噪声，用显式 allowlist 并写明理由。或者删掉 PR 正文的 console error 表述。前者更好，成本一行。

---

## 3. 断言强度逐条对照（含没有断言的部分）

### 3.1 五项都有取值

见 §1.2 表。补充三条容易漏的取值：

- `download["bytes"] == expected.bytes`（`:587`）与 `content-length == str(expected.bytes)`
  （`:597`）是两条独立断言；只改 `content-length` 而字节不变，会在 `:597` 红。
- `download["isValidJson"] is True`（`:588`）只证明下载的是 JSON，不证明内容；内容由
  `sha256` 钉住，不能只看 `isValidJson`。
- API 侧 `_digest(content.content) == ours.sha256`（`:639`）在 Python 里又独立重算了一次，
  与浏览器侧 `node:crypto` 是两条独立的实现路径。

### 3.2 「五项相同」具体是哪五项

PR 正文表格的五项是 Job、Artifact、字节数、SHA-256、契约摘要；测试逐项对应：

| 项 | 页面 | API | 行/对象 | 镜像层复用 |
| --- | --- | --- | --- | --- |
| Job | 身份行 + `status` | `GET /jobs/{id}` 200 + `state` | `speech_jobs.state` | `:682` 同一 `_assert` |
| Artifact | 按钮列表恰一项 | `GET /artifacts/{id}` 200 + `kind/status` | `speech_artifacts` 行 | 同上 |
| 字节数 | 下载 65 字节 | `/content` `content-length: 65` | 对象长度 65 | 同上 |
| SHA-256 | 重算 635601… | `/content` `etag` 与 metadata 同值 | 行 `sha256` 同值 | 同上 |
| 契约摘要 | `/version` 响应体 + 无 alert | `GET /version` 同值 | bundle 含 `CONTRACT_DIGEST` | `:683-688` 另查镜像 `/version` |

镜像层的「五项」不是另写一套断言，而是同一个 `_assert_page_read_the_same_artifact` 跑在镜像
`staged` 栈上；这保证了口径一致，但也意味着 **`_assert` 没断言的项（console error）镜像层也没有**。

### 3.3 没有被断言的东西

- `consoleErrors`（B3）：采集、写入 JSON，但没有 `== []`。
- 镜像 `/` 的 CSP 响应头：`test_the_page_is_served_under_the_deployed_response_headers`
  （`tests/integration/test_workbench_artifact_browser.py:753-762`）走的是 `sandbox.front("/")`，
  即 `_web_tier` 的 Python 复刻层；它断言 `SECURITY_HEADERS` 常量，不读镜像。镜像层只验证页面
  能加载（`pageErrors`/`outbound`），不比较响应头。CSP 回归若仍能渲染（例如少一个
  `frame-ancestors`），这条不会红。
- `download["suggestedFilename"]`：页面固定 `transcript.json`，测试不查；不在五项内，可不补。
- Job 身份行是子串匹配（`:613` `expected.job_id in identityRows`），不是解析 `<dl>` 的逐行值；
  多一个同名前缀的 Job id 才会误判，实际 id 唯一，风险低。
- `test_a_changed_sha256_…` 与 `test_a_read_of_another_jobs_artifact_…` 用裸
  `pytest.raises(AssertionError)`，不校验失败来自哪条断言；不同原因抛 `AssertionError` 也会满足。
  前者随后断言 metadata `sha256 == forged`，把范围收窄了；后者靠第二次 `_assert(ours)` 兜底，
  可接受但不够精确。

### 3.4 反例 1 是「同一份证据换个期望值」，不是「页面读另一 Job」

`test_a_read_of_another_jobs_artifact_is_not_this_ones`
（`tests/integration/test_workbench_artifact_browser.py:694-708`）实际做的是：

1. 让页面读 **ours**（job-page-1 / artifact-1）；
2. 把同一份 evidence 拿去对 **foreign**（job-page-2 / artifact-2）断言，期望 `AssertionError`；
3. 再对 **ours** 断言，期望通过。

这证明「判据对 expected Artifact 的取值敏感」，但不证明「页面读的 Artifact 绑定在所请求 Job 上」：
页面从头到尾读的都是 ours，没有发生「读另一 Job 的制品」。docstring 的
「the judgment binds to Job identity, not to 'some Artifact'」和 PR 正文的「把判定绑到另一条 Job
的 Artifact → 判定红」都比这行测试实际做的多。真正的身份绑定由主用例的
`artifacts == [ours]`（`:613`）+ API `body["artifacts"] == [ours]`（`:646`）承担；我另做了
「页面实际读另一 Job」的变异（§4.1），主用例会红，所以 **覆盖存在，反例的表述不准**。

---

## 4. 变异红/绿证据

全部变异只在 `/tmp/sn952-subject` 内做，跑完用备份 `cp` 还原（不用 `git checkout --` 清他人改动），
每次还原后 `git status --porcelain` 为空。基线先跑一遍：

```
$ env SPEECHNEXUS_TEST_DATABASE_URL=… SPEECHNEXUS_TEST_MINIO_* =… \
    UV_NO_CONFIG=1 uv run pytest -m integration tests/integration/test_workbench_artifact_browser.py -q
.....                                                                    [100%]
5 passed in 18.78s
```

### 4.1 读另一 Job 的制品 → 红

做法：把 `test_the_page_reads_the_same_jobs_artifact` 的 `_probe` 参数从
`job="job-page-1", artifact="artifact-page-transcript-1"` 改成
`job="job-page-2", artifact="artifact-page-transcript-2"`，期望仍是 `ours`。页面实际读了 job-page-2
的 65 字节制品（`OTHER_TRANSCRIPT`），在浏览器摘要处红：

```
$ pytest -m integration "…::test_the_page_reads_the_same_jobs_artifact" -q
E       AssertionError: {'suggestedFilename': 'transcript.json', 'bytes': 65,
  'sha256': 'sha256:0c6f9471…dcfb3a', 'isValidJson': True}
E       assert 'sha256:0c6f9…dcfb3a' == 'sha256:63560…650077f'
tests/integration/test_workbench_artifact_browser.py:586: AssertionError
1 failed in 2.68s
```

**红。** 这条比用例自带的「同一份 evidence 换 expected」更强：它让页面真的读了另一 Job 的制品。

### 4.2 篡改 `sha256` → 红

用例自带的 `test_a_changed_sha256_breaks_the_same_artifact_judgment`
（`tests/integration/test_workbench_artifact_browser.py:711-730`）用 `pytest.raises` 把红包住；
我把这层包去掉，让失败直接露出：

```
E       assert content_reads[0]["etag"] == f'"{expected.sha256}"'
E       assert '"sha256:0c6f9…dcfb3a"' == '"sha256:63560…650077f"'
tests/integration/test_workbench_artifact_browser.py:596: AssertionError
1 failed in 2.68s
```

**红。** `/content` 的 `etag` 跟着被改的行值走，而浏览器落盘字节仍是原摘要；两者不等。

### 4.3 两侧摘要不等 → 页面红

用例自带 `test_a_mixed_contract_digest_is_reported_by_the_page`
（`tests/integration/test_workbench_artifact_browser.py:733-750`）把 `version_override` 设成
`sha256:bbbb…`，页面 `role=alert`。为让测试层也红一次，我把断言取反：

```
E       AssertionError: {'mode': 'identity', 'name': 'mixed-digest', …}
tests/integration/test_workbench_artifact_browser.py:740（变异后 :741）: AssertionError
1 failed in 4.23s
```

从同一轮 probe 的 JSON 取告警原文：

```json
"alerts": [
  "前后端版本不一致这个页面和它连接的接口不是同一版本构建的。……前端契约 ad581b4691e1…，接口契约 bbbbbbbbbbbb…。"
],
"version response body": {"version":"0.1.0.dev0","contract_digest":"sha256:bbbb…"}
```

**红。** 页面确实按「两侧都拿到且不等」告警，不是只存在于代码里；对齐时
`aligned["alerts"] == []` 也通过（`:749-750`）。

### 4.4 对象字节与提交行不一致 → 红（证明浏览器摘要独立）

做法：把 `_seed_committed_job` 的 `put_object` 数据换成 `OTHER_TRANSCRIPT`，行仍报 `ours` 的
`sha256` 与 65 字节。服务端 `etag`/`content-length` 仍是原值，只有浏览器落盘摘要变：

```
E       AssertionError: {'suggestedFilename': 'transcript.json', 'bytes': 65,
  'sha256': 'sha256:0c6f9471…dcfb3a', 'isValidJson': True}
tests/integration/test_workbench_artifact_browser.py:586: AssertionError
```

**红。** 这是「浏览器字节真的重算」的直接证据；如果测试复用 `/content` 的 etag，这条会绿。

### 4.5 注入 console error → 仍绿（B3 的复现）

在 probe 的 `page.goto` 后加 `await page.evaluate(() => console.error("sn952 injected console error"))`：

```
$ pytest -m integration "…::test_the_page_reads_the_same_jobs_artifact" -q
1 passed in 2.65s
$ python -c 'json.load(open("…/same-job.json"))["consoleErrors"]'
['sn952 injected console error']
```

**绿。** 这正是 B3：测试不检查 console error。

### 4.6 setup 失败 → 资源泄漏（B2 的复现）

把 `SPEECHNEXUS_TEST_MINIO_SECRET_KEY` 设成错误值，`make_bucket` 在 `try` 之前失败：

```
E       minio.error.S3Error: S3 operation failed; code: SignatureDoesNotMatch, …
E       bucket_name: sn-page-f07a41aeb76e
1 error in 0.83s
$ psql … -Atc "SELECT nspname … LIKE 'sn_page_int_%'; SELECT rolname … LIKE 'sn_page_%';"
sn_page_int_f07a41aeb76e
sn_page_artifact_f07a41aeb76e
sn_page_jobs_f07a41aeb76e
```

**红（泄漏）。** 已手工清理；收尾时计数 0。

### 4.7 镜像层 + 基线

镜像层用真实 `speechnexus/web-workbench:acf633ff…` 跑 `test_the_page_reads_the_same_jobs_artifact`，
`1 passed in 5.40s`，`CHECK` 行 sha256 与基线一致。还原后 5 条全绿 `5 passed in 17.72s`。

---

## 5. CI 边界

### 5.1 分片收集

`tests/integration/test_workbench_artifact_browser.py` 以 `_browser.py` 结尾，
`scripts/dev-env/integration-shards.py:141-143` 的 `requires_browser` 直接判真；实测完整收集：

```
$ UV_NO_CONFIG=1 uv run python - <<'PY' … collect_integration_nodeids() …
collected: 2867  module cases: 5
shard 8:  browser=True cases=2 [test_a_read_of_another…, test_the_page_is_served…]
shard 9:  browser=True cases=1 [test_a_changed_sha256…]
shard 10: browser=True cases=1 [test_a_mixed_contract_digest…]
shard 11: browser=True cases=1 [test_the_page_reads_the_same_jobs_artifact]
```

与 PR 正文「5 个用例分布在 8–11 分片，均 `browser=True`」一致。

### 5.2 CI 实际跑过，且不是超时源

PR 当前 head 的 CI run `44069784355` 全部检查通过。四个 Web 分片的报告（`gh run download`）显示
新用例的状态全部是 `passed`，没有 skip：

| 分片 | 选中 | 新用例 | 分片耗时 | 新用例 call 耗时（日志） |
| --- | --- | --- | --- | --- |
| 8 | 233 | 2 | 179.04 s | 3.42 s、1.97 s |
| 9 | 226 | 1 | 144.89 s | 1.64 s |
| 10 | 214 | 1 | 106.41 s | 6.12 s |
| 11 | 212 | 1 | 159.09 s | 2.18 s（setup 1.37 s） |

`Gate: integration partitions and merged coverage` 也通过，说明 skip 不是被当成合格；本 PR 的 5 条
在 CI 上是真跑。CI 的 Web 分片会 `pnpm install`、`pnpm build`、`prepare-ci-browser.py` 装 Chromium
（`.github/workflows/ci.yml:663-673`），分片 8–11 的名字就是 `Web + PG/MySQL/MinIO`。环境变量
`SPEECHNEXUS_TEST_MINIO_*` 在 `.github/workflows/ci.yml:704-706` 设置，所以 CI 不会走到 B1 的
skip；但 **本地直接跑** 会。

### 5.3 缺失前置的行为矩阵

| 缺失 | 行为 | 复现 |
| --- | --- | --- |
| PostgreSQL DSN | `ERROR`，`Failed: Configure an isolated PostgreSQL DSN …; skip is not evidence` | `tests/integration/test_workbench_artifact_browser.py:163` |
| `web/workbench/dist` | `ERROR`，`Failed: Build the workbench bundle first: pnpm …` | `:179` |
| `@playwright/test` | `ERROR`，`Failed: Install the web toolchain and its Chromium first …` | `:192` |
| Chromium 二进制 | `FAILED`，probe 退出 1，`assert 1 == 0`（原始 Node 栈） | `:576` |
| MinIO 三元组 | `SKIPPED`，退出 0 | `:168`（B1） |

Chromium 缺失的失败信息是原始 Node 栈（`browserType.launch: Executable doesn't exist`），不是
`_require_browser` 那条友好提示——因为 `_require_browser` 只检查包解析，不检查浏览器二进制。
失败是对的，但可读性可以更好（见 §8）。

### 5.4 稳定性风险

- 新用例本身 1.6–6.1 s，分片 106–179 s，离 job 超时很远；CI 已实跑，不是超时源。
- probe 里有固定 `waitForTimeout(2500)`（identity）和 `800`（read 后），见
  `tests/integration/workbench-artifact-probe.mjs:142`、`:164`。CI 当前绿，但这是时间断言；
  慢 runner 上 identity 响应若晚于 2.5 s，`len(page_version) == 1` 会红。建议用
  `waitForResponse`/`expect.poll` 代替固定睡眠，或把等待接到「收到 `/v1/nexus/version`」上。
- `_probe` 的 `subprocess.run` 没有 `timeout=`（`tests/integration/test_workbench_artifact_browser.py:567-576`）。
  probe 内部各步有 20–30 s 超时，但浏览器启动/Node 卡死时只能等 job 超时；加一个
  `timeout=180` 更稳。

---

## 6. 文档

`docs/operations/same-version-check.md:117` 新增的一句总体与测试范围一致：

- 「真实 PostgreSQL、真实 MinIO、真实 API 组合根与 `web/workbench/dist`」—— 基本成立；组合根是
  真的 `create_app`，但页面在 **replica 层** 请求的 `/v1/nexus/version` 由
  `_web_tier` 的测试替身回答（`tests/integration/test_workbench_artifact_browser.py:355-360`），
  不是真实 API 路由；真实 API 路由由同用例的 `sandbox.get("/v1/nexus/version")`（`:660-661`）和
  可选的镜像层覆盖。说「真实 API 组合根」不算错，但「对端自报的 `contract_digest`」在 replica
  层应理解为「前端层的自报」，不是「API 进程的自报」。
- 「设 `WORKBENCH_PAGE_IMAGE` 时同一判定再经 Web 镜像的 nginx、CSP 与 `/v1/` 代理执行一遍」——
  判定确实复用同一个 `_assert`，页面也确实在镜像 CSP 下渲染；但镜像的 CSP **响应头** 未被比较
  （§3.3），只有页面加载/`pageErrors`/`outbound`。说「CSP 下执行一遍」成立，说「CSP 五项相同」
  就多了。
- 「Job 与 Artifact 行由夹具提交（无引擎推理），因此它证明的是读取路径与同版锚点，不是引擎产出」——
  准确，且与 PR 正文 ① 一致；没有把「测试证明」写成「部署已证」。这一点是达标的。

需要同步修正的文档/陈述：

| 位置 | 现文 | 问题 | 建议 |
| --- | --- | --- | --- |
| `tests/integration/test_workbench_artifact_browser.py:21-31` | 「Missing is an explicit skip」（MinIO） | 与同文件 DB/浏览器分支的 fail 语义、README 的「missing object-storage inputs fail rather than skip」不一致（B1） | 改成失败，docstring 同步 |
| PR 正文「镜像层……`pageerror`/console error/越界请求均为空」 | 三项都声称空 | 测试只断言 `pageErrors`/`outbound`，`consoleErrors` 未断言（B3） | 补断言或删表述 |
| PR 正文「CI 上尚未实际跑过」 | 写在 PR 描述里 | 实际 CI run `44069784355` 已跑过 4 个 Web 分片且 5 条全绿（§5.2） | 更新为实跑结果 |
| `tests/integration/test_workbench_artifact_browser.py:694-708` docstring | 「binds to Job identity」 | 反例 1 只换 expected，不换页面实际读取（§3.4） | 改 docstring，或把 §4.1 的变异做成用例 |

---

## 7. 未验证项

1. **真实引擎产出的 Job/Artifact**：本机无 NVIDIA GPU / vLLM ASR，PR 已声明用夹具行。测试证明的是
   消费侧与同版锚点，不是引擎能产出；这一点我同意 PR 的边界，不算缺陷。
2. **完整 compose 栈**：未起 `deploy/compose.yaml`；迁移 profile、引擎注册/就绪 profile、Web 反向
   代理的组合未复核。镜像层只覆盖 Web 镜像的 nginx/CSP/`/v1/` 代理。
3. **CI 上的镜像层**：CI 没有设 `WORKBENCH_PAGE_IMAGE`，所以镜像层证据是本机手工跑（我用
   `acf633ff…` 复跑通过），不是 CI 门禁。
4. **PR 正文的清理叙事**：`sn951-pg` 删除、`speechnexus-test-minio` 恢复到 `Exited (0)` 等是作者
   当时的环境操作，我没有复核；我用的是自己的 `sn952-review-*` 容器，收尾删除。
5. **CI 上的 console error 实际值**：probe JSON 不随 CI 产物上传，无法从 CI 证明 console error 为空；
   本机基线为空，但注入变异证明测试不会因此红（B3）。
6. **PR 正文的 bundle 树摘要 `sha256:ffb0f712…`**：我按 24 个应用文件逐文件比较，镜像与本机 `dist`
   完全相同；没有复现该具体树摘要（PR 的公式未在测试里，且镜像另带 `50x.html`）。这是证据口径
   差异，不是缺陷。
7. **平台差异**：镜像层的 `_host_ipv4`/`host.docker.internal` 在 macOS Docker Desktop 上通过；
   Linux 开发机是否同样可达未验证（CI 不跑镜像层）。

---

## 8. 非阻断建议

| # | 位置 | 问题 | 建议 |
| --- | --- | --- | --- |
| N1 | `tests/integration/test_workbench_artifact_browser.py:694-708` | 反例 1 是「同一份 evidence 换 expected」，不是「页面读另一 Job」；docstring/PR 正文说得比做得多 | 把 §4.1 的「页面实际读 job-page-2」做成反例，或改名/改 docstring 为「判据拒绝另一个 expected Artifact」 |
| N2 | `tests/integration/test_workbench_artifact_browser.py:753-762` | 「deployed response headers」测的是 `_web_tier` 复刻层的 `SECURITY_HEADERS` 常量，不是镜像；镜像层只查页面能渲染 | 设镜像时 `staged.front("/")` 断言同一组头；或从 `deploy/web/nginx.conf` 读期望值，避免常量漂移 |
| N3 | `tests/integration/test_workbench_artifact_browser.py:355-360` | replica 层 `/v1/nexus/version` 是测试替身，页面同版检查在 CI 里不对真实 API 路由 | 文档写清「replica 层是替身，真实 API 由 direct GET + 镜像层覆盖」；或让替身经真实 API 代理后再改摘要 |
| N4 | `tests/integration/test_workbench_artifact_browser.py:567-576` | `subprocess.run` 无 timeout，probe 卡死只能等 job 超时 | 加 `timeout=180`，失败信息带 probe 名 |
| N5 | `tests/integration/test_workbench_artifact_browser.py:174-197` | 只检查 `dist/index.html` 存在，不检查它是否由当前 checkout 构建；本地陈旧 bundle 可能给出与 CI 不同的结果 | 加构建戳/源码头校验，或在 docstring 明确「先 build」并接受陈旧风险 |
| N6 | `tests/integration/test_workbench_artifact_browser.py:101-112` | `SECURITY_HEADERS` 是 `deploy/web/nginx.conf` 的手抄副本，两边可漂移 | 从 nginx 模板解析，或在镜像层比较实际响应头 |
| N7 | `tests/integration/test_workbench_artifact_browser.py:70` | 从单测 `tests/unit/backend/test_health.py` 导入私有 `_settings` | 搬到 `tests/support/` 或本文件内构造 `Settings` |
| N8 | `tests/integration/test_workbench_artifact_browser.py:259`、`:274` | 两条 Job/Artifact 共用 `request_id='request-browser-1'`、`source_operation_id='operation-browser-1'`，不是生产形状 | 用不同值，或在注释说明只用于读取路径 |
| N9 | `tests/integration/test_workbench_artifact_browser.py:703`、`:719` | 裸 `pytest.raises(AssertionError)` 不校验失败原因 | 给 `_assert` 的失败加可匹配文案，或先断言「page 读到了 /content 200」再 `pytest.raises` |
| N10 | `tests/integration/test_workbench_artifact_browser.py:613` | Job 身份行用子串匹配 | 解析 `<dl>` 行，或断言 `f"任务 {expected.job_id}"` |
| N11 | `tests/integration/test_workbench_artifact_browser.py:537-554` | 清理在 `try` 内正确，但 setup 在 `try` 外（B2） | 用 `ExitStack` 统一生命周期 |
| N12 | `tests/integration/workbench-artifact-probe.mjs:142`、`:164` | 固定 `2500`/`800` ms 等待 | 换 `waitForResponse`/轮询 |
| N13 | `tests/integration/test_workbench_artifact_browser.py:26` | MinIO skip 的 docstring 承诺 | 随 B1 改为 fail |

---

## 9. 验证记录

| 检查 | 结果 |
| --- | --- |
| 基线（5 条，真实 PG/MinIO/Chromium/dist） | `5 passed in 18.78s`；还原后 `5 passed in 17.72s` |
| 镜像层（`WORKBENCH_PAGE_IMAGE=speechnexus/web-workbench:acf633ff…`） | `1 passed in 5.40s`，`CHECK image tier … sha256=sha256:63560121…077f` |
| PG DSN 缺失 | `ERROR`，点名 `SPEECHNEXUS_TEST_DATABASE_URL`，exit 非 0 |
| MinIO 缺失 | `1 skipped in 0.45s`，exit 0（B1） |
| `dist` 缺失 | `ERROR`，点名 build 命令，exit 非 0 |
| Chromium 缺失（`PLAYWRIGHT_BROWSERS_PATH` 空） | `FAILED`，probe exit 1，`assert 1 == 0` |
| 读另一 Job 的变异 | `1 failed in 2.68s`，`sha256 0c6f94… != 635601…` |
| 篡改 `sha256` 的变异（去掉 `pytest.raises`） | `1 failed in 2.68s`，`etag 0c6f94… != 635601…` |
| 两侧摘要不等的变异（断言取反） | `1 failed in 4.23s`，告警含 `前后端版本不一致`、`ad581b4691e1…`、`bbbbbbbbbbbb…` |
| 对象字节与行不一致的变异 | `1 failed in 2.68s`，浏览器摘要变、`content-length` 仍 65 |
| 注入 console error 的变异 | `1 passed`，JSON `consoleErrors=['sn952 injected console error']`（B3） |
| setup 失败（错误 MinIO secret） | `S3Error: SignatureDoesNotMatch`，泄漏 `sn_page_int_f07a41aeb76e` + 两角色（B2），已清理 |
| CI run `44069784355` | 全部检查通过；分片 8–11 的 5 条新用例状态均 `passed`，无 skip |
| 分片收集 | 2867 条，新模块 5 条，分片 8–11，`browser=True` |
| `check-image-contract-digest.py` | `OK: the image's bundle carries this contract revision.` |
| 镜像/本机 24 文件 | 逐文件 SHA-256 相同；镜像另带 `50x.html` |
| 文档门禁 | `RESULT PASS`；`SCOPE terminal problems=0`；`refs=1992 fenced=140 AMBIGUOUS=130 MISSING=65 OK=1792 OUT_OF_RANGE=4`，record 问题不计门禁 |

收尾：两个工作树的 `git status --porcelain` 为空；被审工作树的变异已全部还原；我起的
`sn952-review-pg`、`sn952-review-minio` 容器与桶已删除；`sn_page_int_%`/`sn_page_%` 计数为 0，
`sn-page-*` 桶为空；未触碰主工作区、`sn892-pg`、`speechnexus-coordination-mysql` 或
`speechnexus-test-minio`。
---

## 第二轮复核（head `5b9a19bb`）

**结论：未闭合，不放行。** 第 1、3 项闭合；第 2 项的常规路径闭合，但三个边界仍未闭合：
`make_bucket` 已建桶但客户端报错时 `bucket_created` 仍未置位、清理自身失败会掩盖原始错误并跳过后续
清理、`_serve` 启动超时后 uvicorn 线程未停。下面只审增量，第一轮证据保留在上文。

### R0. 增量口径

`git diff --stat d1885d0e..5b9a19bb` = 1 文件 +125/−95，只改
`tests/integration/test_workbench_artifact_browser.py`；`tests/integration/workbench-artifact-probe.mjs`
未改（`git diff --stat` 可验）。本轮新行号按 `5b9a19bb`；第一轮 §0–§9 的行号仍按 `d1885d0e`。

### R1. 第 1 项 MinIO 缺失：闭合

`_required_environment` 现在先收集缺失变量名，再 `pytest.fail`：

- `tests/integration/test_workbench_artifact_browser.py:168-173`：`missing = [...]` + `pytest.fail`，
  错误信息只列 `missing`，不再 `pytest.skip`。
- 模块 docstring `tests/integration/test_workbench_artifact_browser.py:21-27` 同步为
  「Missing is a failure naming the missing variables」。

实测三变量全缺：

```text
$ env -u SPEECHNEXUS_TEST_MINIO_ENDPOINT -u SPEECHNEXUS_TEST_MINIO_ACCESS_KEY \
      -u SPEECHNEXUS_TEST_MINIO_SECRET_KEY \
      SPEECHNEXUS_TEST_DATABASE_URL=… UV_NO_CONFIG=1 uv run pytest -m integration \
      "tests/integration/test_workbench_artifact_browser.py::test_the_page_is_served_under_the_deployed_response_headers" -q
E  Failed: Configure an isolated MinIO in SPEECHNEXUS_TEST_MINIO_ENDPOINT,
   SPEECHNEXUS_TEST_MINIO_ACCESS_KEY, SPEECHNEXUS_TEST_MINIO_SECRET_KEY;
   missing object storage is not evidence and is never a skip
1 error in 0.45s        # rc=1
```

只缺 secret：

```text
$ env -u SPEECHNEXUS_TEST_MINIO_SECRET_KEY … pytest … -q
E  Failed: Configure an isolated MinIO in SPEECHNEXUS_TEST_MINIO_SECRET_KEY;
   missing object storage is not evidence and is never a skip
1 error in 0.44s        # rc=1
```

**判定：闭合。** 不再有 skip/exit 0；变量点名精确到缺失项，不是笼统列三个名字。

### R2. 第 2 项 setup 清理：常规路径闭合，三个边界未闭合

#### R2.1 已闭合的部分

`_stack` 现在把 `admin`、`pool`、`objects`、两个 server/thread、`container` 都先置 `None`，
`admin = psycopg.connect(...)` 和 `_migrate` 起都在 `try` 内
（`tests/integration/test_workbench_artifact_browser.py:483-563`）；`finally` 按「句柄存在才清理」
（`:564-583`）。作者给的错误 secret 路径实测：

```text
$ SPEECHNEXUS_TEST_MINIO_SECRET_KEY=wrong_secret_for_leak_probe … pytest … -q
E  minio.error.S3Error: … code: SignatureDoesNotMatch … bucket_name: sn-page-e640b597092c
1 error in 0.89s        # rc=1

$ psql … -Atc "SELECT count(*) FROM pg_namespace WHERE nspname LIKE 'sn_page_int_%';
                 SELECT count(*) FROM pg_roles WHERE rolname LIKE 'sn_page_%';"
0
0
$ MinIO list_buckets()（sn-page- 前缀）
[]
```

断言失败路径也验了：把主用例的 `_probe` 指向 `job-page-2` / `artifact-page-transcript-2`，期望仍是
`ours`，`1 failed in 2.61s`（摘要 `635601…` vs `0c6f94…`），事后 schema/角色/桶计数仍为 0/0/[]。
镜像层 `1 passed in 5.57s`，事后同样 0/0/[]，`sn951-page-*` 容器为空。`_serve` 立即失败（模拟）时
`_stack` 的 DB/对象清理也走通，0/0/[]；说明「常规 setup 失败 / 断言失败 / 镜像启动超时」不再泄漏
schema、角色、桶。

#### R2.2 边界一：`make_bucket` 建桶后客户端报错，`bucket_created` 仍为 false

代码顺序是：

```python
objects.make_bucket(bucket)   # tests/integration/test_workbench_artifact_browser.py:516
bucket_created = True         # :518
```

如果服务端已创建桶、客户端因响应丢失/超时抛错，`:518` 不执行；`finally` 的条件
`if objects is not None and bucket_created:`（`:575`）为 false，桶不会被删。用 monkeypatch 让
`Minio.make_bucket` 先真正建桶再抛 `RuntimeError`（模拟 CreateBucket 响应丢失）：

```text
setup error: simulated lost CreateBucket response after the bucket exists
# 事后
pg_namespace sn_page_int_%: 0
pg_roles sn_page_%: 0
buckets: ['sn-page-865bfb8d2aae']      # 残留，已手工删除
```

这正是「建了但没登记」：桶是服务端真实资源，但登记标志只在客户端成功后置位。**未闭合。**
修法：`finally` 只要 `objects is not None` 就对已知 `bucket` 尝试删版本/删桶，容忍 `NoSuchBucket`；
或调用前先置标志并处理 `BucketAlreadyOwnedByYou`，不要只依赖成功返回。

#### R2.3 边界二：清理自身失败会掩盖原始错误并跳过后续清理

`finally` 里 `pool.close()`、`objects.list_objects/remove_*`、`admin.execute(DROP SCHEMA/ROLE)` 没有
逐步 `try/except`（`tests/integration/test_workbench_artifact_browser.py:564-583`）。用 monkeypatch 让
`Minio.list_objects` 在清理时抛错，同时测试体抛 `ValueError("original body failure")`：

```text
raised: RuntimeError cleanup list_objects failed
# 原始 ValueError 被替换
# 事后
sn_page_int_67da17649c5d
sn_page_jobs_67da17649c5d
sn_page_artifact_67da17649c5d
buckets: ['sn-page-67da17649c5d']      # 已手工清理
```

清理异常既掩盖原始错误，又让后面的 `DROP SCHEMA`/`DROP ROLE` 不执行，三个资源全留。**未闭合。**
修法：每个清理步骤单独 `try/except` 收集失败，保证后续步骤继续；最后以原始异常为主，清理失败作为
附加信息（`add_note`/日志），不要用清理异常覆盖它。

#### R2.4 边界三：`_serve` 启动超时后线程未停

`_serve`（`tests/integration/test_workbench_artifact_browser.py:322-335`）启动线程后等
`server.started`，超时在 `:333` 直接 `assert` 抛错；`server`/`thread` 是局部变量，没有在
`except/finally` 里 `should_exit`/`join`。`_stack` 里 `api_server`/`api_thread` 只在 `_serve` 正常返回后
才赋值（`:544`），所以 `finally` 的 `:571-574` 看不到它们。直接用一个 lifespan 挂起的 app 调
`_serve`：

```text
raised: the app did not start
live threads: ['MainThread', 'Thread-1 (run)']
```

DB/对象资源仍会被 `_stack` 的 `finally` 清掉（模拟 `_serve` 立即失败，0/0/[]），但 uvicorn 线程
（以及它持有的 socket）留到进程退出。**未闭合。** 注意：镜像层 readiness 超时是另一条路径，
`_run_image_tier` 的 `try/except BaseException`（`:449-463`）会 `docker rm --force`，这条已修；
作者声称的「启动超时也回收」只覆盖了镜像容器，没有覆盖 `_serve` 的 API/front 启动超时。

#### R2.5 `DROP ROLE` 依赖

正常路径和错误 secret 路径都在 `DROP SCHEMA ... CASCADE` 后 `DROP ROLE`，实测角色计数 0；测试角色
只有 schema 内授权、不拥有对象，未观察到依赖未解。若清理在 `DROP SCHEMA` 前失败，角色会留下
（R2.3 已覆盖）；这不是独立的 `DROP ROLE` 依赖缺陷。

### R3. 第 3 项 `consoleErrors`：闭合

`_assert_page_read_the_same_artifact` 增加
`assert evidence["consoleErrors"] == []`（`tests/integration/test_workbench_artifact_browser.py:647`），
CSP 用例也改成同一断言（`:792`）。注入 `console.error("sn952 injected console error")`：

```text
$ pytest … "…::test_the_page_reads_the_same_jobs_artifact" -q
E  AssertionError: ['sn952 injected console error']
1 failed in 2.66s
```

删掉 `_assert` 里新增的这一行，同一注入：

```text
1 passed in 2.68s
```

证明断言承重。再注入 `console.warn`/`console.info`：

```text
1 passed in 2.67s
```

probe 只把 `message.type() === "error"` 放进 `consoleErrors`
（`tests/integration/workbench-artifact-probe.mjs:77`），warn/info 不进数组，不会误判为红。
CSP 用例仍先逐字比较 `content-security-policy` 头（`:787`），再用空 console 兜底；原来的
`unsafe-eval` 文本过滤被更强的「console 全空 + CSP 头逐字相等」取代，未放开 `unsafe-eval`。
**判定：闭合。** 第一轮 N2（`SECURITY_HEADERS` 是 nginx 手抄副本、镜像头未比较）仍是非阻断建议，
不影响本轮三项判定。

### R4. 本轮实测汇总

| 检查 | 结果 |
| --- | --- |
| 完整文件（新 head，真实 PG/MinIO/Chromium/dist） | `5 passed in 17.70s` |
| 三变量全缺 MinIO | `1 error in 0.45s`，rc=1，点名三个变量 |
| 只缺 secret | `1 error in 0.44s`，rc=1，只点名 secret |
| 错误 secret | `1 error in 0.89s`，`0/0/[]` |
| 断言失败（读另一 Job） | `1 failed in 2.61s`，`0/0/[]` |
| 镜像层 | `1 passed in 5.57s`，`0/0/[]`，无 `sn951-page-*` 容器 |
| 注入 console.error | `1 failed in 2.66s`，`consoleErrors=['sn952 injected console error']` |
| 删断言后同一注入 | `1 passed in 2.68s` |
| 注入 console.warn/info | `1 passed in 2.67s` |
| `make_bucket` 建后抛 | 残留 `sn-page-865bfb8d2aae`，已删 |
| 清理 `list_objects` 抛 | 原始错误被 `RuntimeError` 掩盖，schema/角色/桶残留，已清理 |
| `_serve` 启动超时 | `the app did not start`，`Thread-1 (run)` 仍活 |
| 新 head CI | 所有 required/实际运行的检查 `SUCCESS`（两个 `SKIPPED`：Docs markdown 代码块、Node Controller 镜像矩阵），PR `MERGEABLE` |
| 文档门禁 | `RESULT PASS`；`SCOPE terminal problems=0`；`refs=1999 fenced=141 MISSING=72 OK=1792 OUT_OF_RANGE=4`（record 不计门禁） |

### R5. 本轮未验证

- `make_bucket` 响应丢失/清理失败/`_serve` 超时三个边界都不在 CI 覆盖内；本轮用 monkeypatch 或
  直接调用复现，CI 仍全绿。
- 镜像层仍是本地手工跑（CI 未设 `WORKBENCH_PAGE_IMAGE`）。
- 真实引擎产出的 Job/Artifact、完整 compose 栈仍同第一轮，未验证。
- 作者 PR 正文的 `sn951-pg`/MinIO 清理叙事未复核；我用独立的 `sn952-review-*` 容器，收尾已删除。

---

## 第三轮复核（head `5fbea085`）

**结论：可合（第三轮）。** A/B/C 三个边界均已闭合；修前/修后用同一注入对比，差异落在实现代码上，不是
测试壳。C 的 uvicorn `CancelledError` ERROR 栈只在启动超时路径出现，不改变 pytest/CI 结果；3 次挂起
app 均只剩 `MainThread`，端口 3/3 可复绑。新 head 的 CI 分片在本轮复核时仍在跑，见 R3.6。

### R3.0 增量口径

`git diff --stat 5b9a19bb..5fbea085` = 1 文件 +158/−28，只改
`tests/integration/test_workbench_artifact_browser.py`；probe 未改。第三轮行号按 `5fbea085`。

### R3.1 A：`make_bucket` 建桶后客户端报错：闭合

实现（`tests/integration/test_workbench_artifact_browser.py:607-623`）：

```python
try:
    objects.make_bucket(bucket)
except BaseException as error:
    try:
        _remove_bucket(objects, bucket)
    except BaseException as cleanup_error:
        if not (isinstance(cleanup_error, S3Error) and cleanup_error.code in _NOTHING_CREATED):
            error.add_note("cleanup after make_bucket failed: " + ...)
    raise
bucket_created = True
```

`_remove_bucket`（`:358-360`）先列版本、逐版本删、再删桶；`_NOTHING_CREATED`（`:331-333`）含
`NoSuchBucket`、`SignatureDoesNotMatch`、`AccessDenied`、`InvalidAccessKeyId`。**同一注入**
（monkeypatch `Minio.make_bucket` 先真正建桶再抛）在旧/新 head 上：

```text
OLD 5b9a19bb:
raised: RuntimeError simulated lost CreateBucket response after the bucket exists
notes: []
事后 buckets: ['sn-page-a014141fd3ea']          # 残留

NEW 5fbea085:
raised: RuntimeError simulated lost CreateBucket response after the bucket exists
notes: []
事后 buckets: []                                # 已清
```

旧 head 还残留 schema/角色（第二轮已记录），新 head schema/角色/桶均 0/0/[]。**闭合。**

### R3.2 B：清理失败不掩盖原始异常：闭合

`finally` 现在把每一步放进 `cleanup(name, action)`（`tests/integration/test_workbench_artifact_browser.py:671-713`），
单步失败只追加 `failures`，后续步骤继续；有原始异常时 `original.add_note(...)`，否则
`raise RuntimeError("cleanup failed: " + report) from failures[0][1]`。桶清理先按
`seeded` 里的 `(key, version_id)` 精确删（`:697-699`），再列版本兜底，再 `remove_bucket`。

**同一注入**（`Minio.list_objects` 在清理时抛，测试体抛 `ValueError("original body failure")`）：

```text
OLD 5b9a19bb:
raised: RuntimeError cleanup list_objects failed      # 原始 ValueError 被替换
notes: []
事后 schema/角色/桶: sn_page_int_f8098d210ee7 + 两角色 + sn-page-f8098d210ee7

NEW 5fbea085:
raised: ValueError original body failure              # 原始异常保留
notes: ['cleanup failed after the original error: bucket listing: RuntimeError: cleanup list_objects failed']
事后 schema/角色/桶: 0/0/[]
```

**测试体成功、清理失败**（同一 `list_objects` 注入，`with _stack: pass`）：

```text
raised: RuntimeError cleanup failed: bucket listing: RuntimeError: cleanup list_objects failed
cause: RuntimeError cleanup list_objects failed
事后 0/0/[]
```

`RuntimeError` 点名了失败步骤 `bucket listing`，`__cause__` 保留真实桶删除/列版本失败原因；不是只有
一句笼统的「cleanup failed」。`add_note` 在仓库最低 Python 上可用：`pyproject.toml:8`
`requires-python = ">=3.14,<3.15"`，`BaseException.add_note` 自 Python 3.11 起存在，本机 3.14.9
实测 note 出现在 `exc.__notes__`。**闭合。**

### R3.3 C：`_serve` 启动超时：闭合

`_serve`（`tests/integration/test_workbench_artifact_browser.py:368-420`）现在在自持 event loop 里跑
`server.serve`，超时路径设置 `should_exit`、`call_soon_threadsafe(_cancel_tasks)`、`join(10)`、
`sock.close()`，然后重抛。**同一挂起 lifespan**（`await asyncio.sleep(120)`）在旧/新 head：

```text
OLD 5b9a19bb（一次）:
raised: the app did not start
live threads: ['MainThread', 'Thread-1 (run)']     # 线程残留

NEW 5fbea085（连续 3 次）:
RUN 0 raised: AssertionError: the app did not start
RUN 0 live threads: ['MainThread']
RUN 0 port 61587 rebind: OK
RUN 1 raised: AssertionError: the app did not start
RUN 1 live threads: ['MainThread']
RUN 1 port 61674 rebind: OK
RUN 2 raised: AssertionError: the app did not start
RUN 2 live threads: ['MainThread']
RUN 2 port 61748 rebind: OK
```

**uvicorn `CancelledError` ERROR 栈：** 每次超时 stderr 会打印一段
`ERROR: Traceback ... asyncio.exceptions.CancelledError`（starlette lifespan 在取消时记录）。它只在
**启动已经超时** 的路径出现，正常 5 条用例的 `_serve` 不打印；pytest 此时已经因为
`the app did not start` 失败，ERROR 栈不会改变退出码。`integration-shards.py` 只看 pytest
report/退出码，不 grep `ERROR` 日志，因此不会被 CI 当成额外失败；如果未来加日志门禁，可临时提高
uvicorn log level 或对取消路径静音。**判定：闭合；噪声可接受，不是失败源。**

### R3.4 版本、静态检查与完整套件

| 检查 | 结果 |
| --- | --- |
| `requires-python` / `add_note` | `>=3.14,<3.15`；`add_note` 3.11+，本机 3.14.9 实测可用 |
| 完整文件（新 head，真实 PG/MinIO/Chromium/dist） | `5 passed in 17.45s` |
| 完整文件 + `WORKBENCH_PAGE_IMAGE=…acf633ff…` | `5 passed in 20.24s`，`CHECK image tier …`，0/0/[] |
| 错误 secret | `1 error in 0.86s`，schema/角色/桶 0/0/[] |
| `ruff check tests/integration/test_workbench_artifact_browser.py` | `All checks passed!` |
| `mypy tests/integration/test_workbench_artifact_browser.py` | `Success: no issues found in 1 source file` |
| `check-doc-refs.py` | `RESULT PASS`，`SCOPE terminal problems=0` |

### R3.5 未验证

- 新 head CI 分片在本轮复核时仍 `IN_PROGRESS`（10 `SUCCESS`、2 `SKIPPED`、15 `IN_PROGRESS`），
  作者也未声称通过；不能把本地 5 passed 当作 CI 全绿。
- 镜像层仍是本地手工跑，CI 未设 `WORKBENCH_PAGE_IMAGE`。
- 真实引擎产出的 Job/Artifact、完整 compose 栈同前两轮，未验证。
- 作者 PR 正文的 `sn951-pg`/MinIO 清理叙事未复核；我用独立的 `sn952-review-*` 容器。

### R3.6 非阻断备注

- C 的 `CancelledError` ERROR 栈是超时路径噪声；若未来 CI 对 stderr 做 `ERROR` 门禁，需要静音。
- `_NOTHING_CREATED` 把 `AccessDenied`/`InvalidAccessKeyId` 当作「未创建」；若策略允许建桶但拒绝
  列/删，桶可能残留且没有 note。当前隔离 MinIO 用 root key，未触发；这是权限模型的边界假设。
- `_seed_committed_job` 在 `put_object` 成功、SQL 失败时不会返回 `(key, version_id)`，该版本只能靠
  「bucket listing」兜底；listing 同时失败时 B 的 note 会记录，原始异常仍保留。本轮未观察到单次
  失败泄漏；这是清理链的剩余依赖，不是第三轮 A/B/C 的阻断项。
- 第一轮 N2（`SECURITY_HEADERS` 是 nginx 手抄副本、镜像响应头未比较）仍在，属非阻断建议。
- `_serve` 超时路径的 `thread.join(timeout=10)` 本身不检查 `is_alive`；`_stop_server` 会检查。
  C 的 3 次挂起 app 均按时退出，未观察到 10s 边界问题。

**第三轮结论：可合（第三轮）。**
