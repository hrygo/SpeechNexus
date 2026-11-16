# 前后端同版核对

一次升级如果只换了一半（换了前端没换后端，或回滚了后端没回滚前端），页面会用一套契约去解释另一套契约的返回，任务结果与制品就可能读错或读不到。本页给出可观察的判据：运行期能自己看见的检查，以及两侧都没有该检查时的人工核对清单。

判据分两类，不要混用：**契约摘要**回答「两侧是否按同一版机器契约生成」；**构建事实**（镜像 label、产物树摘要、源码树）回答「镜像是不是这个提交构建的」。

## 运行期信号

接口新增 `GET /v1/nexus/version`（契约见 `contracts/speech-openapi.yaml`）：

```bash
curl -sS http://<部署地址>/v1/nexus/version
# {"version":"0.1.0.dev0","contract_digest":"sha256:…"}
```

`contract_digest` 是机器契约源（`contracts/` 下每个 `*.yaml` 与 `*.json`）的摘要，由契约生成器产出，同时写进 Python 与 TypeScript 两个生成消费者。响应带 `Cache-Control: no-store`：这是构建事实，缓存过的答案会让混搭看起来一致，生成校验器因此把该响应头当作契约的一部分。

浏览器侧（`admin` 与 `workbench` 两个入口都装了同一守卫）在页面启动时把这个页面**构建时烘焙**的摘要与接口自报摘要比较：

| 接口表现 | 页面表现 |
| --- | --- |
| 摘要相等 | 不显示任何提示（正常状态） |
| 摘要不同 | 顶部出现 `role="alert"`「前后端版本不一致」，列出两侧摘要前 12 位 |
| `404`（旧接口没有该路由） | 提示「无法确认前后端同版」——可能是只升级了一半 |
| 应答不符合生成契约（含被缓存、字段缺失） | 提示「无法确认前后端同版」 |
| 完全不可达、或应答不是 JSON（例如静态服务器回退 `index.html`） | 不提示：这不能证明混搭 |

浏览器里的告警元素带 `data-nexus-contract-check="mismatch|absent|unconfirmed"`，自动化可以据此判定，而不必依赖文案。

离线重算与比对（在你要核对的提交上执行）：

```bash
UV_NO_CONFIG=1 uv run python scripts/dev-env/print-contract-digest.py
UV_NO_CONFIG=1 uv run python scripts/dev-env/print-contract-digest.py --expect sha256:…
```

## 什么时候只剩人工核对

- **整套部署都早于该校验**：前端与接口两侧都没有检测代码，页面不会提示，系统不会拒签。判据是 `GET /v1/nexus/version` 返回 `404`，且页面 bundle 里搜不到 `data-nexus-contract-check`。这种情况下用下面的清单，并在发布说明里写明本次是同版核对过的。
- **没有浏览器**：只跑 API 的自动化里没有页面可看，但 `contract_digest` 仍可 curl 出来比对（第 3 项）。

## 人工核对清单

四项各自对应一个构建事实，逐项都有判据；缺一项就意味着「同版」还没有被证明。命令中的 `<sha>` 换成你要核对/发布的提交。

### 1. 镜像来源 label（构建期）

```bash
docker image inspect --format '{{index .Config.Labels "org.speechnexus.source.commit"}} {{index .Config.Labels "org.speechnexus.source.dirty"}}' \
  speechnexus/web-workbench:<sha>
```

判据：label 等于 `<sha>`，且 `dirty` 为 `0`；`admin` 与 `workbench` 两个镜像的 label 必须相同。Python 镜像目前不带 label（`deploy/images/python.Dockerfile` 没有 `LABEL`），所以 API 一侧的来源以构建记录里的提交为准，判据是该提交与两个前端镜像的 label 相同。构建命令见 [deploy/README.md](../../deploy/README.md) 的「Web images」。

### 2. `web-dist` 树摘要（构建期）

```bash
docker build --target build -f deploy/images/web.Dockerfile \
  --build-arg APP_NAME=workbench -t speechnexus/web-dist:check . \
&& docker run --rm --entrypoint sh speechnexus/web-dist:check -c \
  'cd /workspace/web-dist && find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum'
```

判据：与同一提交在本机 `pnpm --filter @speechnexus/workbench build` 得到的 `dist` 树同摘要、同文件数，说明镜像里的页面就是这个提交编译出来的（该命令与判据的由来见 `deploy/README.md`）。

### 3. 契约摘要（构建期 ↔ 运行期，可直接对照）

```bash
curl -sS http://<部署地址>/v1/nexus/version | python3 -c 'import json,sys; print(json.load(sys.stdin)["contract_digest"])'
UV_NO_CONFIG=1 uv run python scripts/dev-env/print-contract-digest.py
grep -o 'sha256:[0-9a-f]\{64\}' web/workbench/dist/assets/*.js | sort -u
```

判据：三个值必须一致——部署自报值、核对提交上的重算值、页面 bundle 里烘焙的值（bundle 里还会出现测试夹具的假摘要，形如重复字符，不是这一项）。这一项就是运行期判据的离线版本，因此也是四项里唯一能直接跨「构建期/运行期」对照的一项。

### 4. 后端源码树（构建期）

判据：API 镜像构建记录里的提交等于 `<sha>`，且与两个前端镜像 label 相同。需要内容级证据时，对镜像内已安装的 `speechnexus_backend` 与同一提交的工作树用同一算法逐文件 `sha256` 后比较总数与逐项值（`#829` 的做法是 690 个 `.py` 逐文件相同）；两者相同才说明镜像里的后端就是这个提交的源码。

## 这套判据为什么够用

- **运行期判据把「同版」变成页面自己能看见的事实。** 摘要相等意味着前后端由同一版机器契约生成：接口返回的字段、状态与错误码在两侧是同一份定义，这正是「用旧规则解释新返回」会发生的唯一原因。摘要不等、路由缺失或应答违约时页面立刻告警，不再静默。
- **摘要是按字节算的，这是有意的保守取舍。** 契约文件里的注释、空白改动同样会改变摘要，两侧于是必须同批重建；宁可多一次重建，也不放过真正的差异。摘要不等时提示只说「不一致」，不判断差异大小。
- **它不声称证明源码相同，也不需要。** 契约没变而实现变了（修 bug、改日志）不改变线上协议，按定义就是兼容的；`GET /v1/nexus/version` 暴露的 `version` 只用于人读，判据是摘要。
- **人工清单覆盖运行期判据拿不到的那一半。** 整套部署都早于该校验时没有检测代码可跑，四项构建事实是唯一的依据；它们各自钉住一个环节（来源提交、页面产物、契约、后端源码），任一项不成立就说明存在混搭，结论是「不得当作同版消费」。
- **边界仍然明确**：该校验不拒绝请求、不做版本协商。混搭时页面会告警，但接口仍会照常应答；是否继续使用由操作者决定。

## 相关

- 契约与生成消费者：[contracts/README.md](../../contracts/README.md)、[packages/contracts/README.md](../../packages/contracts/README.md)。
- 部署构建命令与构建窗口判据：[deploy/README.md](../../deploy/README.md)。
- 发布就绪判定表的 F-CLIENT 行：[MVP 发布就绪判定表](../plans/mvp-release-readiness.md)。
