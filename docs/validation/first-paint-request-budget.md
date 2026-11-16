# 首屏请求预算与唯一登记例外

页面在用户显式操作之前不访问业务 API。这条规则原先只存在于测试断言里，没有任何文档写过它；#853 引入首屏同版身份检查（`GET /v1/nexus/version`）时，因此撞上了一族没有归属的「零请求」断言（#901 P1）。

本文是这条规则的正式出处和断言账本。规则本身写在 [web/AGENTS.md](../../web/AGENTS.md)；本文件逐门禁记账，说明每处断言原来的意图、身份请求为什么不违反它，以及新增第二个首屏请求时哪一层会转红。

## 规则

**页面在用户显式操作前不访问业务 API。首屏同版身份检查是唯一登记例外：每次页面加载恰好一次 `GET /v1/nexus/version`，不带凭据、不写数据、不触发业务读取。**

判据（任一条不满足即视为越界）：

- 路径必须是 `/v1/nexus/version`，不得是业务前缀。
- 次数：每次页面加载一次；单页内重挂载可以再次出现，但每次都必须是同一个路径。
- 分类：已配置的 API 只读请求。不计入 `mutation_requests`、不计入 `outbound_requests`（不是外发）、不产生写入。
- 应答不可达、404、5xx、非 JSON 时，界面保持沉默（不得显示版本告警）。

## 断言账本

「零请求」断言分三层。三层都可以容纳这**一个**例外，但容纳方式必须是登记，不是放宽判据：判据仍然只接受这一个路径，换成别的路径或业务请求照样转红。

### 一、合成边界：探针与浏览器规格

| 位置 | 断言（原文意图） | 登记方式 |
| --- | --- | --- |
| `scripts/dev-env/admin-task-recovery-probe.mjs:156` | `recovery must not automatically access any API`：恢复态不自动拉业务数据 | 业务计数 `requests` 原文不动；身份请求单独计 `identityRequests`，路由表登记该路径 |
| `scripts/dev-env/workbench-file-host-probe.mjs:267,412,472` | `reload must not automatically access an API`：刷新/恢复不自动拉 job | 同上，业务计数不动 |
| `scripts/dev-env/workbench-file-demo-probe.mjs:163` | `No service request is allowed from the fake scenario`：假场景里没有任何服务请求 | 同上；`unexpected` 语义不变（身份路径在路由表里，不进 `unexpected`） |
| `scripts/dev-env/workbench-manuscripts-probe.mjs:521-523` | 损坏恢复页「业务 0 请求」 | 业务 `requests` 仍是 `assert.equal(requests, beforeDamaged)`；新增「身份恰好 1 次、URL 精确等于声明路径」 |
| `scripts/dev-env/admin-task-center-probe.mjs:171`、`admin-task-events-probe.mjs:283`、`admin-workspaces-probe.mjs:195`、`workbench-tts-probe.mjs:150`、`workbench-tts-host-probe.mjs:301,438,462` | `unexpected` 为空 / 请求数不变：这些页面只许打脚本化过的路由 | 身份路径登记进各自路由表并单独记账；`unexpected` 判据不变 |
| `scripts/dev-env/workbench-media-probe.mjs:192` | `unexpected network requests`：不出网 | 媒体探针不经 `/v1/`，无首屏请求，未改 |
| `scripts/dev-env/admin-profiles-live-probe.mjs:205`、`workbench-asset-reconcile-probe.mjs:216`、`workbench-manuscripts-probe.mjs:413`、`workbench-realtime-asr-probe.mjs:746` | 写入数 / 请求数不变：操作后不得多发 | 计的是写与业务请求，身份读取不进入这些计数，未改 |
| `tests/web/file-task.behavior.spec.mjs:174,229,258,304,…` | `calls.unexpected` 为空：页面只许打脚本化过的路由 | `calls` 增加 `identity` 数组；`unexpected` 判据不变；`afterEach` 要求每个用例都只见过声明路径 |
| `tests/web/render-refusal.behavior.spec.mjs` | 未脚本化路径 `route.fallback()`：只拦工作台前缀 | 身份路径 fallback 到 preview，应答 HTML → 沉默，未改 |
| `tests/web/deployed-headers.deployed.spec.mjs` | 首屏无 console error、无告警 | console error 只放行该 URL；新增「首屏必须发出这一次身份请求」（计数）与「错摘要必须渲染 mismatch 告警」（正向装配证据） |

### 二、真实进程：集成浏览器门禁

`tests/integration/test_workbench_orphan_probes.py`（15 个）与 `test_workbench_realtime_synthetic_probes.py`（9 个）驱动上表的探针；`tests/integration/workbench_real_runtime.py` 的 Tier 与四个 harness（chapter-create、assistant、manuscripts、voice-catalogue、diagnostics）装配真实组合根。这些 harness 都不跑语音底座，而生产 `deploy/web/nginx.conf` 把 `/v1/nexus/version` 路由给语音底座，因此它们按部署事实自行应答同一个身份（`tests/integration/bundle_identity.py`，`version_identity_body` + 生成摘要）。deletion / meeting 两个 harness 直接装配 workbench 应用，同样登记该路径，否则守卫的 `role=alert` 会与页面自己的告警在同一页撞车。

### 三、单元测试

`web/**` 里的「挂载不发请求」断言共 92 处，分布在 18 个子组件/子系统的 Host 测试里。它们渲染的是子树（`FileTaskHost`、`TtsTaskHost`、`ManuscriptHost`、`ProfileHost`…），不是 `App` 根；身份检查只挂在 `App` 根上，所以这些断言看不到它，也无需改动。机械判据：

```sh
grep -rln 'from "\./App"\|from "\.\./App"\|<App />' web --include=*.test.tsx --include=*.test.ts
```

只命中 5 个文件，其中 3 个与请求有关，均已显式登记：

| 文件 | 断言 | 处理 |
| --- | --- | --- |
| `web/admin/src/task-center/TaskCenterHost.test.tsx` | 挂载 `App` 后 `fetch` 调用列表 | 由「零调用」改为「只有 `["/v1/nexus/version"]`」并写明任务中心仍等显式读取 |
| `web/workbench/src/contract-identity.test.tsx` | 守卫自身的 4 条 | 覆盖 mismatch / 同版沉默 / 不可达沉默 / 与常驻横幅风格可区分 |
| `web/workbench/src/meetings/MeetingNavigation.test.tsx` | `unexpected synthetic route`：路由表列全页面可发出的请求 | 显式登记该路径，其他路径仍是 `throw` |
| `web/admin/src/App.test.tsx` | 无请求断言（只断言渲染内容） | 未改 |
| `web/admin/src/contract-identity.test.tsx` | 守卫自身 11 条 | 覆盖同版/错版/404/5xx/违约/缓存/超时/释放与「只问一次」 |

客户端函数测试（`workspace-client`、`profile-client`、`workbench-client`、`realtime-client`、`voice-transport`、`deletion-core` 等）直接调用函数，不渲染 `App`，`fetch` 断言针对被调用的那一次客户端行为，与首屏无关。

## 新增第二个首屏请求时会转红的地方

| 想放宽成 | 会红在哪 |
| --- | --- |
| 任意首屏请求都被允许 | 探针 `unexpected` / 路由表 `assert`；`assertOnlySameVersionRequests` 要求 URL 精确等于 `/v1/nexus/version` |
| 首屏请求算进业务计数 | 探针的 `assert.equal(requests, before…)`；`workbench-manuscripts-probe` 的「业务 0」 |
| 删掉告警组件而门禁不动 | `deployed-headers` 的两条新断言：首屏必须发出这一次请求；错摘要必须渲染 mismatch |
| 把 404 / 不可达当成不一致告警 | `contract-identity` 的沉默用例；`deployed-headers` 的「无接口应答时不得出现告警」 |

## 边界

- 该规则覆盖浏览器页面。服务进程之间的调用、CLI 与探针自身的请求不在其列。
- 这个例外只解决一类混搭：两侧都带该端点、但契约修订不同。`#853` 的原始反例（旧消费者静默读到新 Artifact）与「新页面 + 真旧接口」都不在本设计能覆盖的范围内，见 [前后端同版核对](../operations/same-version-check.md) 的「明示限制」。
- 身份检查只在页面加载时比对一次；页面开着期间后端换版不会重查，也不会告警。需要运行期变更感知时另行设计。
- 工作台 API 单独升降级（没有跨版本契约差异时）不会告警：判据比较的是契约摘要，容器版本号或提交哈希不同的两侧只要契约一致，就视为同版。
