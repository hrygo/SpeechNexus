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

浏览器侧（`admin` 与 `workbench` 两个入口都装了同一守卫）在页面启动时把这个页面**构建时烘焙**的摘要与接口自报摘要比较。**只有两侧摘要都拿到且不相等才告警**；拿不到一侧的值时是「未知」，不是「不一致」：

| 接口表现 | 页面表现 |
| --- | --- |
| 摘要相等 | 不显示任何提示（正常状态） |
| 摘要不同 | 页面顶部、主内容之上出现 `role="alert"`「前后端版本不一致」，列出两侧摘要前 12 位 |
| `404`（反向代理、静态服务器、或旧接口没有该路由） | 不提示：这条应答不能说明运行的是哪个契约修订 |
| `403` / `5xx` / 应答不符合生成契约（含被缓存、字段缺失） | 不提示：同为「未知」 |
| 完全不可达、超时（>5 s）、或应答不是 JSON（例如静态服务器回退 `index.html`） | 不提示：离线首屏不得弹出版本告警 |

告警元素带 `data-nexus-contract-check="mismatch"`，自动化据此判定而不依赖文案；告警与常驻的「共享部署」说明条在样式上刻意不同（实心白底 + 4 px 玫红左标线 vs 浅琥珀底 + 1 px 边框），位置也不同（告警在头部下方、主内容之前，常驻说明在 hero 之后）。实测位置（两个入口一致）：1280×560 视口下告警 `top=100`、`bottom=168`；360×800 视口下 `top=116`、`bottom=272`，均不被视口裁切，横向溢出为 0。

离线重算与比对（在你要核对的提交上执行）：

```bash
UV_NO_CONFIG=1 uv run python scripts/dev-env/print-contract-digest.py
UV_NO_CONFIG=1 uv run python scripts/dev-env/print-contract-digest.py --expect sha256:…
```

## 什么时候只剩人工核对

- **整套部署都早于该校验**：前端与接口两侧都没有检测代码，页面不会提示，系统不会拒签。判据是 `GET /v1/nexus/version` 返回 `404`，且页面 bundle 里搜不到 `data-nexus-contract-check`。这种情况下用下面的清单，并在发布说明里写明本次是同版核对过的。
- **只升级了一半、但新页面那侧读不到旧接口的身份**：运行期判据拿不到接口摘要时按「未知」处理，页面不告警。这类混搭只能靠下面第 3 项（curl 对照 bundle 烘焙值）发现，判据是接口 `404` 而 bundle 里有 `data-nexus-contract-check`。
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

第三条命令读的是本机 `dist`；要在**镜像**上做同一判断（例如 `docker load` 进来的跨机构建产物，或不在本机的构建机产物），用 `scripts/dev-env/check-image-contract-digest.py <镜像>` 代替：它从镜像发布的 bundle 里取出烘焙值，与本工作树重算值比对，摘要缺失、多个候选或不相等都返回非 0，用法见 [deploy/README.md](../../deploy/README.md) 的「Moving a built image to another host」。无工作树时用 `--expect`，它只能取两种来源之一：**在要核对的那个提交上离线重算的值**，或**构建机/发布记录里的摘要**；不能取被检查部署自己 `GET /v1/nexus/version` 报的值——那是这套部署给自己作证（把 bundle 改掉、label 不动，再拿改后的值当 `--expect`，`EXIT=0`），取错来源时这个检查等于没做。

### 4. 后端源码树（构建期）

判据：API 镜像构建记录里的提交等于 `<sha>`，且与两个前端镜像 label 相同。需要内容级证据时，对镜像内已安装的 `speechnexus_backend` 与同一提交的工作树用同一算法逐文件 `sha256` 后比较总数与逐项值（`#829` 的做法是 690 个 `.py` 逐文件相同）；两者相同才说明镜像里的后端就是这个提交的源码。

## 这套判据为什么够用

- **运行期判据把「同版」变成页面自己能看见的事实。** 摘要相等意味着前后端由同一版机器契约生成：接口返回的字段、状态与错误码在两侧是同一份定义，这正是「用旧规则解释新返回」会发生的唯一原因。摘要不等、路由缺失或应答违约时页面立刻告警，不再静默。
- **摘要是按字节算的，这是有意的保守取舍。** 契约文件里的注释、空白改动同样会改变摘要，两侧于是必须同批重建；宁可多一次重建，也不放过真正的差异。摘要不等时提示只说「不一致」，不判断差异大小。
- **它不声称证明源码相同，也不需要。** 契约没变而实现变了（修 bug、改日志）不改变线上协议，按定义就是兼容的；`GET /v1/nexus/version` 暴露的 `version` 只用于人读，判据是摘要。
- **人工清单覆盖运行期判据拿不到的那一半。** 整套部署都早于该校验时没有检测代码可跑，四项构建事实是唯一的依据；它们各自钉住一个环节（来源提交、页面产物、契约、后端源码），任一项不成立就说明存在混搭，结论是「不得当作同版消费」。
- **边界仍然明确**：该校验不拒绝请求、不做版本协商。混搭时页面会告警，但接口仍会照常应答；是否继续使用由操作者决定。

## 明示限制

**这条判据只解决一类混搭：两侧都带 `GET /v1/nexus/version`、但契约修订不同。** 它不解决、也解决不了另外两类，不要读成「混搭都可见了」：

- **`#853` 的原始反例「旧消费者静默读到新 Artifact」修不掉。** 旧页面里没有这段检测代码，任何客户端判据都无能为力；只能靠下面的构建事实清单（第 1、2、4 项）在部署前判定。
- **「新页面 + 真旧接口」同样不告警。** 旧接口对该路径返回 `404`，而 `404` 在「未知」一侧（反向代理、静态服务器、旧接口给的是同一条 404，无法据此判定在跑哪个契约修订），所以页面保持沉默；这类混搭只能靠第 3 项（curl 自报值 vs bundle 烘焙值）发现。

其余限制：

- **只在页面加载时比对一次。** 页面开着期间后端换版不会重查、也不会告警；需要持续感知时刷新页面或按第 3 项核对。
- **工作台 API 单独升降级不告警。** 判据只问语音底座的 `GET /v1/nexus/version`，而工作台页面由另一个进程提供 `web/dist` 与 `/v1/workbench/*`；工作台 API 换了契约修订、语音底座没换时，两侧摘要仍相等。这类错配由工作台自己的契约测试与发布流程覆盖，不在本判据范围内。
- **`404` 不再细分「对端是不是我们的 API」。** 实测一个没有该路由的构建返回：`404`、`content-type: application/json`、`x-request-id: <32 hex>`、无 `cache-control`、体是框架默认的 `{"detail":"Not Found"}`（不是公共 `ErrorResponse` 信封）。要把它和静态服务器的 404 区分开，前端必须把「框架默认 404 体」硬编码进判据——那是契约没有声明、我们也不拥有的协议细节，且旧镜像永远只会给这一种答复，任何客户端形状校验都不可能回溯。**若要彻底消除这一类，正确做法在部署侧**：让反向代理只把该路径转给语音底座（不要落到静态 `location /`），使这条 404 只能来自 API；用户看到信号则改由第 3 项核对。

### 已知无法区分的那一类（实测）

| 情形 | 对端实际答复 | 页面 | 唯一可用的判据 |
| --- | --- | --- | --- |
| 新页面 + 旧接口（无该路由） | `404`，框架默认 JSON 体 | 静默 | 第 3 项 curl 对照 bundle 烘焙值 |
| 新页面 + 反向代理/静态服务器 404 | `404`，HTML 或纯文本 | 静默 | 同上（自报值对不上） |
| 两侧同契约修订 | `200` + 相同摘要 | 静默 | 无需核对 |

## 相关

- 契约与生成消费者：[contracts/README.md](../../contracts/README.md)、[packages/contracts/README.md](../../packages/contracts/README.md)。
- 部署构建命令与构建窗口判据：[deploy/README.md](../../deploy/README.md)。
- 发布就绪判定表的 F-CLIENT 行：[MVP 发布就绪判定表](../plans/mvp-release-readiness.md)。
