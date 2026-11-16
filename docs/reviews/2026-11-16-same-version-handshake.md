---
title: "PR #896 非作者审查：运行期同版判据（#853）"
status: active
version: "0.1.0"
date: 2026-11-16
---

# PR #896 非作者审查：运行期同版判据（#853）

审查对象：PR [#896](https://github.com/hrygo/SpeechNexus/pull/896) `feat/same-version-handshake`，
head `5b1f6985`，关联 issue [#853](https://github.com/hrygo/SpeechNexus/issues/853)。
审查开始时基线是 `origin/main` = `8b290445`，审查期间主干前进到 `579df7f4`（`#898`，仅
`docs/plans/mvp-release-readiness.md` 两行）——两者之间 `contracts/`、`backend/`、`web/`、
`tests/` 逐字节相同，故下文全部证据对新旧基线同时成立；但 `#898` 与 `#896` 都改
`docs/plans/mvp-release-readiness.md` 的判定表，作者 rebase 时需处理这处文本冲突。
审查分支 `review/896-same-version` 基于 `origin/main` = `579df7f4`，本报告是该分支与 `origin/main`
的唯一差异（`git diff origin/main...HEAD --stat` = 1 file）。

复核环境：独立工作树 `/private/tmp/sn902`（被审 head）+ `/private/tmp/sn902-main`（`origin/main`，用于
新旧对照与「真实旧 API」）；`UV_NO_CONFIG=1 uv sync --locked --all-packages --all-groups`、
`pnpm install --frozen-lockfile`、`pnpm build`、真实 Chromium（Playwright 1.64.1，本机缓存）。本机 macOS
无 GPU，本片与引擎无关，未尝试真实引擎。

## 1. 结论

**不能合。** 判据本身是真判据、不是摆设：作者给的三组浏览器实测全部独立复现（第 6 节），摘要确实是
单一生成产物、确实覆盖 37 份机器契约、`no-store` 确实发出、失败分支确实不误报。阻断项在别处——这一片
**新增了一次首屏 API 请求**，而「页面在用户显式操作前不访问 API」是仓库里被 22 处断言执行、却从未写成
规则的一条边界；作者只清理/放宽了其中 2 处，其余 20 处全部变红。CI 红不是环境问题，不是抖动。

| 阻断 | 事实（全部实测） |
| --- | --- |
| **P1-1** | head 上 `tests/integration/test_workbench_orphan_probes.py` **11/15 探针失败**；同一工作树把身份请求关掉后 **15/15 全绿**，`origin/main` 同环境 **15/15 全绿** → 全部由新增的首屏请求与未登记路由引起 |
| **P1-2** | head 上 `pnpm test:behavior` **9/18 失败**（`calls.unexpected` 收进 `GET /v1/nexus/version`）；同样关掉身份请求后 **18/18 全绿**。本文件作者未改 |
| **P1-3** | 合成路由表未登记该路径：`workbench-manuscripts-probe.mjs` 记 6×`Unexpected synthetic route`，并 `45 !== 44`（`requests` 计数） |
| **P2-1** | 判据只问 **speech API**；workbench 页面的 `/v1/workbench/*` 面由另一个进程（workbench API）服务，该进程没有这条路由 → workbench API 单独升级/回滚时页面**不告警**（第 5 节） |
| **P2-2** | 只比一次：页面开着时后端被换成另一版，**20 s 内 0 次重查、无告警**（实测） |
| **P2-3** | 静态文件服务器对 `/v1/nexus/version` 回 404 → 页面报「可能只升级了一半」的 **误报**（实测）；文档把这类归入「不提示」 |
| **P2-4** | 告警与常驻「共享部署」横幅**样式逐项相同**且紧随其后 48 px；1280×560 与 360×800 下告警被视口裁切；文档写「顶部出现」，实测在首屏下段（1280×720 时 top=511） |
| **P2-5** | `deployed-headers` 放宽后允许 **0 条**过滤错误：把告警组件从 `App.tsx` 删掉重新构建，门禁**仍然全绿**（实测）——布线消失无法被发现 |

## 2. 边界冲突：是既有守卫过严，还是新请求越界

这一节回答「首屏发一次 API 请求」这件事本身越过了什么。结论先给：**不是安全问题上的越界，也不是
守卫过严可以一笔带过；它越过了一条被广泛执行、但从未写成规则的边界，因此处置必须是「写成规则 + 逐门禁
记账」，不是删断言，也不是撤功能。**

### 2.1 有文档写过「首屏零请求」吗

- **没有全局规则。** `grep` 全仓 `docs/**`、`web/AGENTS.md` 的「首屏 / 零请求 / 不联系 API / 不发起请求」，
  唯一相关的是 `docs/prd.md:502` 的「诊断**显式读取**、无来源的数据保留未知」——那是诊断面板的读面原则，
  不是「整页零请求」。
- **有一条更窄、且被本次改动完整保住的书面不变量。** `docs/reviews/2026-11-13-terminal-exit.md:94` 立的是
  「恢复数据损坏 → `FileTaskSession` 从未构造，`upload`/`create`/`fetch` 零调用，`removeItem` 从未执行」。
  我核了代码路径：`journal.load()` 抛错 → `initialize()` 返回 null → 渲染「恢复信息不可用」，会话未构造。
  身份探测不构造会话、不读写业务数据，**这条不变量没有被违反**。
- **没有任何文档写过「页面首屏不发 API 请求」，但代码里有 22 处断言在它之上。** 其中两处的断言消息直接
  把它写成产品规则：
  - `scripts/dev-env/admin-task-events-probe.mjs`：`Production host accessed API without explicit action`
  - `scripts/dev-env/workbench-file-demo-probe.mjs:141`：`No service request is allowed from the fake scenario`

所以：**规则写在测试里、没写在文档里。** 这不是「守卫过严」——六条消息里有三条是不可再降的业务判据
（生产宿主不得在无显式动作时访问 API、假场景不得有任何服务请求、恢复损坏前不得联系 API）；也不是纯粹的
「越界」——首屏比对恰恰要发生在任何业务读取之前，放首屏是有道理的（页面还没读数据就先确认同版，正是
#853 想要的顺序）。正确处置是承认这是**有意的例外**并写成规则，见 P1 修法与 P2 建议。

### 2.2 越过的边界的完整代价（实测）

同一台机器、同一 `pnpm build` 产物、同一个 pytest 调用：

| 工作树 | `test_workbench_orphan_probes.py` | `pnpm test:behavior` |
| --- | --- | --- |
| `/private/tmp/sn902-main`（`origin/main`） | **15 passed** | 未跑（CI 主干绿） |
| `/private/tmp/sn902`（head `5b1f6985`） | **11 failed, 4 passed** | **9 failed, 9 passed** |
| `/private/tmp/sn902`，把 `useContractIdentity` 的 `compareIdentity(...)` 换成恒 `{kind:"unreachable"}` 后重建 | **15 passed** | **18 passed** |

第三行是关键：只停掉那一次请求，两处全绿。红因归属清楚，不是环境、不是抖动、不是别的改动。

被打破的断言逐条（head 上实跑）：

```
workbench-file-demo-probe.mjs:141      "No service request is allowed from the fake scenario"
admin-task-events-probe.mjs            "Production host accessed API without explicit action"
admin-task-recovery-probe.mjs:148      assert.equal(requests, 0)
workbench-file-host-probe.mjs:257      assert.equal(requests, 0)
workbench-tts-host-probe.mjs:287       assert.equal(requests, 0)
workbench-tts-probe.mjs:128            deepEqual ['Unexpected network request'] vs []
admin-task-center-probe.mjs:149        deepEqual ['Unexpected service or outbound request'] vs []
workbench-prompter-probe.mjs           deepEqual ['/v1/nexus/version','/v1/nexus/version'] vs []
admin-workspaces-probe.mjs             deepEqual ["ENOENT … web/admin/dist/v1/nexus/version"] vs []
workbench-voice-create-probe.mjs       deepEqual ["ENOENT … dist/v1/nexus/version", …]
workbench-manuscripts-probe.mjs:473    AssertionError: 45 !== 44（并 6×Unexpected synthetic route）
file-task.behavior.spec.mjs:199…541    calls.unexpected = ['GET /v1/nexus/version', …]（9 例）
```

`ENOENT …/dist/v1/nexus/version` 说明请求已经逃出探针的合成边界、落到 vite preview 的静态目录解析上——
探针的「意外请求」台账正是在拦这件事。

### 2.3 作者的清理程度

作者动了两处，都合理但都不够：

- `web/admin/src/task-center/TaskCenterHost.test.tsx`：`expect(fetch).not.toHaveBeenCalled()` → 「恰好一次且
  URL 精确等于 `/v1/nexus/version`」。判据上这是**等价收紧**（多一个请求就会变成两项数组、立刻失败），
  不是放宽；但用例名仍写 `without automatically accessing any service`，与断言矛盾（P3-1）。
- `tests/web/deployed-headers.deployed.spec.mjs:160-179`：console 门禁从「零 console error」改为「除该 URL
  外零 console error」。定向且带 `≤1` 上限（我实测：两个探测组件 → 门禁变红），但这处放宽允许 0 条，
  等于对「布线消失」失明（P2-5）。

**没动的 20 处就是 CI 红。** 作者自述「实测通过」不成立——至少 `pnpm test:behavior` 与
`tests/integration/test_workbench_orphan_probes.py` 在 head 上是红的，且都能在本机复现。

## 3. 请求数类断言清单与原始意图

| 位置 | 原始意图（读得出/读不出业务语义） | head 状态 | 作者处理 |
| --- | --- | --- | --- |
| `web/admin/src/task-center/TaskCenterHost.test.tsx:43` | 「`<App/>` 挂载不自动访问任何服务」（用例名原文） | 绿（已改） | ✔ 改为恰一次命名请求 |
| `tests/web/deployed-headers.deployed.spec.mjs:174` | 「部署策略下首屏零 console error」 | 绿（已放宽） | ✔ 放宽，但有 0 条漏洞 |
| `tests/web/file-task.behavior.spec.mjs:104,199…541` | 脚本化 HTTP 边界的**闭合性**：未脚本化的请求即失败 | **红 9 例** | ✘ 未动 |
| `scripts/dev-env/workbench-manuscripts-probe.mjs:124,144,473` | 同上（合成路由表）+「恢复损坏不得联系 API」 | **红** | ✘ 未动 |
| `scripts/dev-env/workbench-file-demo-probe.mjs:141` | 「假场景不得有任何服务请求」 | **红** | ✘ 未动 |
| `scripts/dev-env/admin-task-events-probe.mjs` | 「生产宿主未显式动作不得访问 API」 | **红** | ✘ 未动 |
| `admin-task-recovery-probe.mjs:148`、`workbench-file-host-probe.mjs:257`、`workbench-tts-host-probe.mjs:287` | 首屏 `requests == 0` | **红** | ✘ 未动 |
| `admin-task-center-probe.mjs:149`、`workbench-tts-probe.mjs:128`、`workbench-prompter-probe.mjs`、`admin-workspaces-probe.mjs`、`workbench-voice-create-probe.mjs` | 同上（各探针自建台账） | **红** | ✘ 未动 |
| `tests/web/render-refusal.behavior.spec.mjs:303…721` | **业务**调用计数（`calls.executions`、`calls.generations`） | 绿 | 无需处理（语义计数，不受影响） |
| `web/**` 其余 18 处 `expect(fetch)…`（`profile-client` 9、`workbench-client` 4、`workspace-client` 2、`realtime-client`/`voice-transport`/`deletion-core` 各 1） | 客户端类单测，不渲染 `<App/>`，断言的是「这次调用发了几次请求」 | 绿 | 无需处理 |

处置口径（我独立判断后同意作者应做、但必须按此口径做）：**不把身份请求从 `requests` 里排除**，而是
(a) 业务请求计数一字不改，(b) 身份检查单独计数并断言「恰好 1 次且 URL 精确等于 `/v1/nexus/version`」，
(c) 该路径登记进合成路由表并归类为非变更（`mutation_requests` 不得 +1），(d) 一次性扫完上表。作者给的
四条要求方向正确；缺的是覆盖范围，不是方向。

## 4. 告警可见性与时机

### 4.1 几何实测（真实 Chromium，`web/workbench/dist`，旧 API 触发 `absent`）

| 视口 | 告警 top/bottom | 完整在视口内 | 说明 |
| --- | --- | --- | --- |
| 1440×900 | 511 / 591 | ✔ | `scrollY=0` |
| 1280×720 | 511 / 591 | ✔ | |
| 1280×600 | 511 / 591 | ✔ | |
| 1024×640 | 505 / 585 | ✔ | |
| 768×700 | 583 / 686 | ✔ | |
| 1280×560 | 511 / 591 | ✘ | 底部 31 px 被裁 |
| 360×800 | 685 / 860 | ✘ | 底部 60 px 被裁（两列变一列，说明文字读不全） |

其他可见性判据：`display/visibility/opacity` 正常，**无任何隐藏祖先**（逐层检查 `display:none`、
`visibility:hidden`、`opacity:0`、`content-visibility:hidden`、`hidden`、`aria-hidden`），
`document.elementFromPoint(中心)` 命中告警自身（没被固定头部盖住），`role="alert"` 与
`data-nexus-contract-check` 都在，`scrollY=0`（不需要滚动才到得了首屏），也没有被懒加载挡住——
组件直接渲染在 `AppShell` 的 children 首位、在主 bundle 里（首屏就发出请求即证据）。

**结论**：桌面视口「首屏可见」成立，不是摆设；但**不是文档说的「顶部」**，而是 hero + 既有边界横幅之后
的首屏下段；小视口与移动端会被裁一部分。`docs/operations/same-version-check.md` 的「顶部出现」应订正。

### 4.2 显著性问题（作者未提）

告警直接用常驻横幅同一个 class `.boundary-note`。实测两者计算样式**逐项相同**：

```
alertStyle  == bannerStyle == {background: oklch(0.96 0.04 75), border: oklch(0.5 0.14 75 / 0.28),
                              borderRadius: 16px, marginTop: 48px, padding: 16px 20px, fontSize: 16px}
labelStyle  == {color: oklch(0.5 0.14 75), fontWeight: 700, textTransform: uppercase}
```

即在首屏上，用户看到的是两块**外观完全一致**的黄色横幅相隔 48 px：第一块是永远存在的「共享部署」
说明，第二块才是真实告警。这正是「有告警但被当成静态说明」的典型形态。建议给告警独立的视觉身份
（图标、更强的前景/边框对比、或置顶 sticky），并让 `.boundary-note` 只属于常驻横幅。

### 4.3 时机：只比对一次（P2）

页面加载 → 1 次 `/v1/nexus/version` → 之后永不复查。实测：先用本分支 API 打开（无告警），再把同源上游
换成 `origin/main` 的 API（真实旧进程），等 20 s：

```json
{"longLived": true, "identityRequestsOnMount": 1, "beforeSwap": {"kind": null},
 "afterSwap": {"kind": null, "identityNodes": 0}, "serverIdentityHitsTotal": 8}
```

`serverIdentityHitsTotal: 8` = 前面 7 次视口加载 + 这次 1 次；换后端之后 **新增 0 次请求**，页面保持静默。
作者自述只承诺「不一致时不静默」，但这个场景是「页面开着时发生混搭」，且是 #853 的原始缺陷形态之一
（浏览器留着旧 bundle、后端被升级/回滚）。按派单口径记为 **P2**：不要求本片做长轮询，但必须把
「首屏命中即视为整段会话同版」这个假设写进文档，并建议在首次业务读取前再比对一次（成本一次 GET，
却把判据钉在真正要用数据的时刻）。

### 4.4 失败分支实测：不误报，但有一类误报

| 场景 | 告警 | 身份请求 | console errors |
| --- | --- | --- | --- |
| 网络不可达（上游 502/连接拒绝） | 无 | 1 | `Failed to load resource: 502` |
| CSP `connect-src 'none'` 拦掉请求 | 无 | **0**（未发出） | 2 条（违规 + `Fetch API cannot load`，`location` 指向 JS 资源） |
| 应答是静态服务器的 HTML（200） | 无 | 1 | — |
| **静态文件服务器对路径回 404** | **`absent`「可能这次只升级了一半」** | 1 | `Failed to load resource: 404` |

网络不可达 / CSP 拦截 / abort 都**不会**误报版本不一致——作者这条用例是真的。但「404」这条规则是
**只看状态码、不看应答形状**（`contract-identity.tsx:81-85` 在 `content-type` 判定之前就返回 `absent`），
于是任何没有把 `/v1/` 代理到 API 的静态托管（`python -m http.server`、`vite preview`、CDN 直出静态站点）
都会在首屏弹一个「只升级了一半」的吓人告警。仓库自己已经知道这个坑：`tests/web/deployed-server.mjs`
特意对 `/v1/`、`/v2/` 回**可解析的 503**，注释写的就是「让页面走自己的错误态，而不是踩上文件服务器的
非 JSON 404」。判据与这条既有约定相反，属 P2：建议只有**API 的 404 信封**才算 `absent`，HTML/纯文本
404 记为 `unreachable`；同时订正文档那一行。

## 5. 新路由边界

### 5.1 判据覆盖的是哪一侧（P2）

反代配置把 `/v1/nexus/version` 交给 **speech API**（`deploy/web/nginx.conf` 的 `location /v1/`），而
`location ~ ^/v[12]/workbench/` 交给 **workbench API**——一个独立组合根、独立服务（还带
`profiles: ["workbench-api"]`），且它**没有**这条路由（`workbench/app.py` 只挂业务路由与
`/v1/workbench/status`）。

页面消费面实测：

```
admin   : 全部 /v1/nexus/*（jobs、workspaces、admin/profiles）        → 判据完全覆盖
workbench: /v1/nexus/*（jobs、assets）+ /v1/workbench/*（projects、transcripts、books、
           chapters、voices、teleprompter、meetings）+ /v1/realtime    → 判据只覆盖前者
```

所以 workbench 入口那句「前后端同版本」只对 speech API 成立。两个 Python API 在 `docker compose build`
时来自同一 context、同一 Dockerfile、同一提交，正常情况下同版；但它们是**可独立构建/重启/回滚的两个
服务**（workbench-api 还是 profile 开关的），一旦只换其中一个，页面会拿到「相等」而继续用旧 workbench
契约解释新返回——正是 #853 要消灭的形态。必须收窄陈述（或让 workbench API 也自报摘要、页面两侧都核）。

### 5.2 其余边界（通过）

- **`no-store` 真的发出**：`curl` 实测 `cache-control: no-store`，且响应契约把该头写成 `required` +`const`；
  生成校验器在任何一侧缺失/被改写时拒绝（`tests/contract/test_version_identity_http.py` 的
  `test_a_cached_response_is_rejected`）。前端另外用 `cache: "no-store"` + 校验应答头，代理缓存导致漏检
  这条路被堵了两层。
- **限流/授权**：全仓没有全局限流器，约定是各写面自带 ingress 容量；该路由是常数时间、无 I/O、无身份、
  无正文，与 `/healthz` 同级，不需要新增。首期无登录，公开面无授权概念可加。
- **route-conformance 与生成消费者**：`contracts-route-conformance` 通过（新增路由与实现一致）；路由进了
  `bindings.json` 的 `version` profile、`speech.generated.ts`、`profile_registry`、`version_http.py`、
  `version-http.ts`，`tests/contract/test_asset_http_contract.py:69` 与
  `test_operational_contract.py:223` 的闭合清单同步加了它。
- **泄漏**：`version` 与 `/healthz` 同值（早已公开），`contract_digest` 是不含路径与内容的哈希；响应体
  只有这两个字段（`additionalProperties: false`，多字段被校验器拒绝）。没有内部路径、身份或凭据。
- **405 与无依赖可用性**：`POST` 得公开 `405` 信封（`allow: GET`），DB/对象存储不可用时仍 `200`——都实测过，
  这决定了一个只配了一半的部署仍能回答身份问题。
- 唯一小瑕疵：路由挂载在 `create_health_router` 内（为了让组合根不必再走一条装配路径），注释已解释，
  静态检查的「占位路由不得吞掉字面路由」守卫仍通过，可接受。

## 6. 摘要覆盖面与误报

### 6.1 覆盖哪些文件（逐条实测）

`contract_digest()`（`scripts/dev-env/generate-contract-bindings.py:823-845`）对 `contracts/` 下**每个
`.json` 与 `.yaml`**（递归）做「相对路径 + 空格 + 该文件 sha256 + 换行」再取 sha256。当前 37 份文件全部
覆盖：`speech-openapi.yaml`、`admin-openapi.yaml`、`workbench-openapi.yaml`、`operations.openapi.yaml`、
`bindings.json`、`coordination-ownership.json`、全部 `*.schema.json`、`internal/*`、`shared/*`。只有
`contracts/README.md` 与 `contracts/AGENTS.md` 被有意排除。

我在工作树里逐次改动并重算（每次改完 `git checkout --` 还原）：

| 改动 | 摘要 | 判定 |
| --- | --- | --- |
| 基线 | `sha256:43127fc96f94d43e8ac03cb67cb2fe53f76eefdd50814b3ff1fc5cf48b68bcd4` | — |
| `contracts/README.md` 追加一行（散文） | 不变 | 有意排除 ✔ |
| `contracts/AGENTS.md` 追加一行（散文） | 不变 | 有意排除 ✔ |
| `admin-openapi.yaml` 仅追加一个换行 | `sha256:5793b732796ca2f2280e732a9dba874ff7c3819089d323187e850746360e37c4` | 变 ✔ |
| `shared/errors.schema.json` 追加一个空格 | `sha256:8118411e65a7cab20351487526331374581464ec1e4ec09a6d8d859c2bae81c6` | 变 ✔ |
| `internal/zero-dispatch.schema.json` 追加空格 | `sha256:faaa87b1f306a312c5242549bb94bc47859d11612ee2a7a37ce5c3aef2450aac` | 变 ✔ |
| 新建 `contracts/hypothetical.yml` | 不变 | **未覆盖**（今天仓库无 `.yml`；见 P3-2） |

「两次语义不同的契约同摘要」不可构造：路径与逐文件字节都进摘要，改名/增删/改一个字节都会变。

### 6.2 是不是同一份生成产物

是。`generate()` 里同一个 `digest` 变量同时写 `packages/contracts/src/contract_digest.generated.ts` 与
`packages/contracts/src/speechnexus_contracts/contract_digest.py`（`generate-contract-bindings.py:978-990`），
`make contracts-drift` 保证两者与 `contracts/` 逐字节一致，`tests/contract/test_version_identity_http.py`
的 `test_both_generated_digests_equal_a_fresh_computation` 每次都重算并三方比对。我另外从三个真实产物侧
核了一遍，同一个值：

```
contracts 重算   : sha256:43127fc96f9…bcd4
Python 常量      : sha256:43127fc96f9…bcd4    (speechnexus_contracts.contract_digest.CONTRACT_DIGEST)
TypeScript 常量  : sha256:43127fc96f9…bcd4    (contract_digest.generated.ts)
构建后 bundle    : sha256:43127fc96f9…bcd4    (web/workbench/dist/assets/index-*.js)
运行期接口       : sha256:43127fc96f9…bcd4    (GET /v1/nexus/version)
```

（bundle 里另有一处 `sha256:aaaa…`，是既有 `export_artifact_demo` 夹具，与本次判据无关；运维文档也已
提醒「bundle 里会出现重复字符的假摘要」。）

### 6.3 字节级的保守会不会自伤

不会。`make contracts-generate` 只写 `packages/contracts/`，**不回写 `contracts/`**：跑完
`UV_NO_CONFIG=1 make contracts-generate` 后 `git status` 只有 `uv.lock`（环境副作用，不入库），摘要
仍是 `43127fc9…`。因此唯一的「预期内变更」也不会制造误报；反过来，仅改注释/空白就会要求两侧同批重建，
这是有意取舍（文档已写明），属可接受成本。

## 7. 混搭复现（两侧输出，独立构造）

四条场景都用真实 Chromium 打开**本分支构建产物**（除第 4 条用 `origin/main` 构建产物），同源反代
`/v1/nexus/version` 到真实进程。旧 API 是 `origin/main` 工作树里跑起来的真实 `create_app` 进程
（`/healthz` 200、该路由 `404 {"detail":"Not Found"}`）。

**① 同版（新页面 + 新 API）——零噪音**

```json
{"identityRequests": ["…/v1/nexus/version"], "consoleErrors": [], "pageErrors": [],
 "alertCount": 0, "bodyTextLength": 752, "title": "SpeechNexus 语音工作台"}
```

admin 入口同结果（`alertCount: 0`、`consoleErrors: []`）。作者「AFTER 同版零噪音」成立。

**② 混搭①（新页面 + 旧 API）——`absent`**

```json
{"alertKind": "absent", "role": "alert", "ariaLabel": "前后端同版本核对",
 "rect": {"top": 511, "bottom": 591, "width": 1200}, "fullyInViewport": true, "hitTestInside": true,
 "hiddenAncestors": [],
 "text": "无法确认前后端同版 接口没有提供版本信息，可能这次只升级了一半（页面或接口其中一侧没有跟着换）。
          这种状态下无法确认页面与接口是否同一版本，读任务结果和制品前请让部署方核对前后端是否同批升级。",
 "consoleErrors": [{"text": "Failed to load resource: … 404 (Not Found)", "url": "…/v1/nexus/version"}],
 "pageErrors": []}
```

admin 入口同结果。（作者给的是 `alertKind="absent"`、`apiVersionStatus=404`，我这次两侧一致。）

**③ 混搭②（新页面 + 另一契约修订的 API）——`mismatch`，两侧摘要可读**

```json
{"alertKind": "mismatch", "role": "alert", "fullyInViewport": true,
 "text": "前后端版本不一致 这个页面和它连接的接口不是同一版本构建的。混着用的时候，页面会按旧规则解释
          新接口的返回，读任务结果和制品就可能读错或读不到。请让部署方把前端和后端一起升级，或者一起回滚。
          前端契约 43127fc96f94…，接口契约 bbbbbbbbbbbb…。"}
```

（构造方式：页面用本分支产物，上游对同一路径回另一修订的摘要；摘要是真的来自契约字节的格式，
不是改判据代码。）

**④ BEFORE（旧页面 + 新 API）——仍然静默，边界如实**

用 `origin/main` 构建的 bundle + 本分支 API：`identityRequests = 0`、无告警、无 console error。即 #853
的原始缺陷只对「构建于本改动之后」的页面消失；两侧都早于本次改动时仍无判据（作者文档已写明，属已知
边界，不计缺陷）。

## 8. 必须修清单

### P1（阻断合并）

1. **把 CI 拉回绿，且不许用删断言的方式。** 20 处红断言逐个处理：业务请求计数一字不改；身份检查
   单独计数并断言「恰好 1 次且 URL 精确等于 `/v1/nexus/version`」；`/v1/nexus/version` 登记进
   `workbench-manuscripts-probe.mjs` 的合成路由表与 `file-task.behavior.spec.mjs` 的 `installBackend`，
   归类为非变更（`mutation_requests` 不得 +1）。判定基线：本报告 §2.2 的三行对照——改完后
   `test_workbench_orphan_probes.py` 应 15 passed、`pnpm test:behavior` 应 18 passed，且上面 11 个探针里
   的每一条「零请求」断言仍在（只是被拆成 biz/identity 两个计数器）。
2. **把这条边界写成规则。** 在 `docs/operations/same-version-check.md` 或 `web/AGENTS.md` 写清：页面首屏
   现在会发**恰好一次**同源 `GET /v1/nexus/version`，它不读业务数据、不构造会话、离线行为不变；因此
   「首屏零 API 请求」这条既有断言从此有唯一例外，其余请求仍为零。否则下一个人会在三个方向里任选一个
   乱改（加白名单、删断言、撤功能）。

### P2（合并前须给出结论或收窄陈述）

3. **判据只覆盖 speech API**：workbench 页面的 `/v1/workbench/*` 面由独立进程服务且该进程无此路由。要么
   让 workbench API 也暴露同一路由、页面两侧都比对，要么在 PRD/运维文档与 PR 正文里把「前后端同版本」
   收窄成「本页与 speech API 来自同一契约修订」。
4. **只比一次**：把「首屏命中即视为整段会话同版」写进文档；建议在首次业务读取前再比对一次。
5. **静态服务器 404 误报**：只有 API 的 404 信封才算 `absent`，HTML/纯文本 404 记为 `unreachable`；
   同步订正运维文档里「静态服务器回退 → 不提示」那一行（当前只写了 200 回退的情形）。
6. **`deployed-headers` 的 0 条漏洞**：把 `toBeLessThanOrEqual(1)` 改成恰好 1，并断言被过滤的那条是
   资源加载失败（我今天实测：删掉告警组件后门禁仍绿）。
7. **告警的可见性与显著性**：给告警独立于 `.boundary-note` 的视觉身份；小视口/移动端要么置顶要么缩短
   文案；订正文档「顶部出现」的措辞（实测 1280×720 时 top=511，1280×560 与 360×800 会被裁）。

### P3（可跟进）

8. `web/admin/src/task-center/TaskCenterHost.test.tsx:37` 用例名仍写 `without automatically accessing any
   service`，与断言矛盾：改名（例如「挂载只做一次身份核对，任务中心仍等显式读取」），保留后半句的原意。
9. 摘要不覆盖 `.yml`，也不覆盖任何将来新增的其它机器契约扩展名。建议加一条自检用例：「`contracts/`
   下每个文件要么进摘要，要么在显式排除名单（`README.md`、`AGENTS.md`）里」。
10. `role="alert"` 是插入时新建的元素，没有预先存在的 live region；主流 SR 会播报，严格可访问写法是先有
    `aria-live` 容器再填充。可记入已知限制或加一个常驻容器。
11. 补一条正向用例：`absent`/`unconfirmed` 分支下「身份请求恰好 1 次且 URL 精确」，避免将来被静默剔除。

## 9. 已复核通过的项（无需改动）

- 6 个契约门禁逐个执行全绿：
  `CONTRACT_BASE_REF=origin/main make contracts-speech-changes contracts-http-changes contracts-compat
  contracts-route-conformance contracts-drift contracts-check`（exit 0，`contract validation: passed (37 files)`）。
- `make lint`、`pnpm typecheck`、`pnpm format:check`、`pnpm test:a11y`（52 passed）、
  `pnpm --filter @speechnexus/admin test`（159 passed）、`pnpm --filter @speechnexus/workbench test`
  （1078 passed）全绿；契约与后端用例 29 passed。
- `no-store` 实际发出、缓存应答被拒、405 信封、无依赖仍 200、路由进 conformance、生成消费者齐全。
- 摘要单一产物、覆盖 37 份机器契约、散文不影响、`contracts-generate` 不制造漂移。
- 同版零噪音、混搭两侧告警与文案、CSP/断网不误报——全部复现。
- 作者的「按字节保守」取舍与运维文档的能力边界陈述（不拒签、不协商、两侧都旧时靠人工清单）准确。

## 10. 复核脚本与证据位置

- 工作树：`/private/tmp/sn902`（head）、`/private/tmp/sn902-main`（`origin/main`，用于真实旧 API/旧页面）。
- 浏览器harness（未入库）：`/private/tmp/sn902-harness.mjs`、`/private/tmp/sn902-visibility.mjs`；
  它们只服务同源反代 + 真实 Chromium，不改任何被测源码。
- 变异证据：把 `useContractIdentity` 的 `compareIdentity(...)` 替换为恒 `{kind:"unreachable"}` 后重建，
  两处红门禁同时转绿；把 `<ContractIdentityAlert />` 从 `web/admin/src/App.tsx` 删除后重建，
  `pnpm test:deployed` 仍 7 passed。两处变异均已还原（`git status` 仅剩环境的 `uv.lock`）。
