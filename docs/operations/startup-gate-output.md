# 启动闸门的失败输出形态

每一处「拒绝启动」的判定都只有两种可维护的形态：在**同步装配**或**入口脚本**里先说结论
（一行具名 `FAIL:`，或一行本单元自己的具名消息，然后非零退出），或者把它留在 **ASGI
lifespan** 里由 uvicorn 结束进程（traceback + 退出码 `3`）。两种都可以是对的，含糊才是
错的：运维只看容器日志，无法从「重启循环」里推断该改哪个键、该跑哪条命令。

## 收录规则（口径先写清）

- **收录**：任何能让进程在开始服务前退出的判定——同步装配、ASGI lifespan、入口 `main()`
  自身的校验，以及一次性 init 容器的启动。前端容器（`web` / `admin`）不含这类判定，不收。
- **一个单元一行**：同一进程里判定不在同一阶段的，分行记（例如 `api` 的同步结算预检与
  lifespan 的 Job 迁移闸门）。
- **「实测」的边界**：本表每行的真实输出都是 `2026-11-17` 在本机 worktree（`origin/main`
  `c33891ab` 起、rebase 到合入 `#924` 之后）上启动真实入口得到的，不是从代码推断；仍然复现
  不了的行标「未验证」，不写实测结论。
- **两栏区别**：「真实输出（实测）」写产品实际打印的东西；「文档承诺」只写**运维文档**承诺
  的输出形态，文档没承诺就写「未承诺输出形态」并据此给出处置，不把「代码里写了」当承诺。

`tests/ci/startup_gates.py` 是这张**表**的唯一来源（它是测试文件，不读产品源码）：`tests/ci/
test_startup_gate_output_map.py` 从它渲染表格并逐字比对下文，改一侧不改另一侧即红。同一处
还做两件机械判据：

1. 每个「承诺输出形态」的行都必须点名一个真的启动过入口的用例；用例不存在即红。
2. **逐字引用的产品文案必须能在它声明的产品文件里找到**（引文是测试对产品的断言，不是
   测试自己的事实）；`external:` 前缀只用于 uvicorn、alembic 这类第三方消息，并显式声明。

## 读法

- **同步装配**：uvicorn `config.load()` 调用 ASGI factory 的阶段。异常从 factory 抛出，穿过
  `uvicorn.run()` 回到入口的 `try`，因此入口可以打印结论并 `SystemExit(1)`。
- **ASGI lifespan**：`Server.startup()` 里 `await self.lifespan.startup()` 之后
  `if self.lifespan.should_exit: sys.exit(3)`（uvicorn 0.54.0）。这个 `sys.exit(3)` 是
  `BaseException`，入口的 `except <具体配置异常>` 到不了这里，所以输出必然带 traceback，
  退出码是 `3` 而不是 `1`。
- **入口 `main()`**：进程自己在进入任何服务循环之前的校验。它能不能给出一行结论，取决于它
  自己有没有包住那段校验。

## 关于「不泄露」的准确边界

**只对「一行结论」类单元成立**：它们的输出不含 DSN 片段、主机、库名、用户名，点名的对象是
键名、桶名、schema 与表名（表内逐行记录了点名了哪些）。例如 `job-dispatch-worker` 入口闸门
连接失败时只打印异常类型名与人话，不回显 psycopg 原文——因为 psycopg 的连接失败**不带**
SQLSTATE（口令错、端口关闭、库不存在、角色不存在四种都实测 `sqlstate is None`），而它的消息
含服务器地址与登录名。

**traceback 类单元不满足这一点，本表也不作此承诺**：`api` 自身 lifespan 在真口令错时会打印
驱动原文（含 `connection to server at "…", port …` 与角色名），`gateway` 控制面与
`job-migrate` 同理。任何「启动失败不泄露凭据」的通稿都不成立；能主张的只有「一行结论类单元
不泄露部署输入」。

## 表格

<!-- startup-gate-output:start -->

| 单元 | 阶段 | 真实输出（实测） | 文档承诺 | 一致 | 处置 |
| --- | --- | --- | --- | --- | --- |
| `api`（`speechnexus-api`）· Job 迁移与角色闸门 | ASGI lifespan（`job_schema_preflight` 的 `require_migrated_schema`） | uvicorn 打印 `ERROR: Traceback …` 与 `Application startup failed. Exiting.` 后以退出码 `3` 结束；没有 `FAIL:` 行；末行异常消息就是 `JOB_PREREQUISITES` 的 `schema_not_migrated` 原文 `The Job database has not run the migration profile: a required relation is missing, so no Job can be stored.` | `compose-minimal.md` §9.1：启动期是 traceback + 退出码 3、**不是**一行 `FAIL:`；一行 `FAIL:` 只属于同步装配的结算预检 | 是 | `#924` 已把文档写成实测为真；本卡补进程级用例钉住「traceback + 退出码 3 + 无 FAIL 行」，并把 `async-job-runbook.md` §1 行 2 的过期承诺改成同一句。若要把它挪到入口换一行 `FAIL:`，须另立卡（启动顺序与二次连接） |
| `api` · 同步 TTS 结算预检 | 同步装配（`speech_runtime.build_speech`） | 一行 `FAIL:`，内容是 `the settled-capacity preflight cannot read the admission tables` 加关系名与迁移命令；退出码 `1`，无 traceback | `async-job-runbook.md` §8 表：日志行 `FAIL: the settled-capacity preflight …`、退出码 `1` | 是 | 补进程级用例钉住（`tests/integration`，需要隔离 PostgreSQL 的空 schema） |
| `api` · 私有配方加载（TTS 与转写两个 loader） | 同步装配（factory 内 `load_tts_recipe` / `load_transcription_recipe`） | 实测（TTS 配方）：`Traceback …` 后跟 `Invalid private speech admission recipe` 与绝对路径，退出码 `1`；转写 loader 用同一包装抛 `Invalid private transcription admission recipe`，未单独起进程复现（结算预检先于它拒绝空 schema） | `compose-minimal.md` §9 与 `async-job-runbook.md` §5 只写「API 启动报 recipe digest / 同名原因」，未承诺输出形态与退出码 | 是（无格式承诺） | 改文档：在本表承认 traceback、退出码与绝对路径。理由：这条失败来自部署方手改的私有文件，消息已点名失败类；包一层 `FAIL:` 只会重复同一句，还会把「配方不可用」与「库未迁移」两类混淆 |
| `api` · 设置校验 | 入口 `main()`（`Settings()`） | `Traceback …` 后跟 pydantic 的 `ValidationError`，退出码 `1` | `compose-minimal.md`：某些键非法时「stops API startup」，未承诺输出形态 | 是（无格式承诺） | 改文档：在本表承认 traceback 与退出码。理由：pydantic 已点名键与规则，且这段在任何 uvicorn 装配之前运行 |
| `api` · 其余 lifespan 运行时（binding list / catalogue / audit 等） | ASGI lifespan（`run_in_threadpool(<runtime>.open)`） | uvicorn 打印 `ERROR: Traceback …`（形如 `<runtime> startup rejected`）后以退出码 `3` 结束，无 `FAIL:` 行 | 无（文档只描述这些身份与它覆盖的读面） | 是（无格式承诺） | 改文档：承认 lifespan 阶段一律 traceback + 退出码 `3`。理由：uvicorn 0.54.0 的 `Server.startup()` 在 lifespan 失败时 `sys.exit(3)`，入口的 `except` 到不了那个 raise 点 |
| `execution-gateway`（`speechnexus-gateway`）· 设置校验 | 入口 `main()`（`Settings()`） | `Traceback …` 后跟 pydantic 的 `ValidationError`，退出码 `1` | `compose-minimal.md` §9 只点名原因文案，未承诺输出形态 | 是（无格式承诺） | 改文档：承认 traceback 与退出码。理由：uvicorn 之前的配置拒绝，没有第二个可执行阶段可挪 |
| `execution-gateway` · 文件语音配置（四条 lane） | 同步装配（factory `create_app` 的 `load_file_speech_configuration`） | `Traceback …` 末行是 ValueError 与 `invalid gateway file ASR configuration`，退出码 `1` | `compose-minimal.md` §9：`gateway 退出并报 invalid gateway file ASR configuration`，未承诺输出形态 | 是（无格式承诺） | 改文档：承认 traceback 与退出码，说明原因文案在 traceback 末行。理由：拒绝发生在同步装配阶段，改写成一行 `FAIL:` 会新增一条要单独维护的承诺 |
| `execution-gateway` · 控制面（barrier / 查询 / ingress）装配 | ASGI lifespan（`_build_control`） | 控制面数据库不可达时 uvicorn 打印 `ERROR: Traceback …` 与 `Application startup failed. Exiting.` 后以退出码 `3` 结束，无 `FAIL:` 行；traceback 内含驱动原文 | 无（文档未承诺控制面装配失败时的输出形态） | 是（无格式承诺） | 改文档：由本行把「控制面失败是 traceback + 退出码 3」写成实测结论，不再与 VAD 文案共用「未验证」 |
| `execution-gateway` · 固定 CPU VAD 运行时 | ASGI lifespan（`silero_onnx` 初始化） | 未验证：需要镜像内的 Silero ONNX 与固定依赖，本机 worktree 无法复现 | `compose-minimal.md` §9：`gateway 报 cannot initialize the pinned CPU VAD runtime` | 未验证 | 记录为未验证项：只差这一句文案的实测；同阶段的控制面失败已由本表 `gateway_control` 行实测为 traceback + 退出码 `3` |
| `node-controller`（默认 Compose 不启动） | 入口 `main()`（`Settings()`）与 factory | `Traceback …` 后跟 pydantic 的 `ValidationError`（设置）或 factory 拒绝，退出码 `1` | `docs/components/node-controller.md`：启动即校验配置，未承诺输出形态 | 是（无格式承诺） | 改文档：承认 traceback 与退出码。理由：本单元不在默认 Compose 内，没有部署文档承诺失败形态 |
| `asset-inspector`（`speechnexus-asset-inspector`） | 入口 `main()`（`InspectionSettings.from_env` 与运行时路径检查） | 一行 `asset inspector configuration error:` 加具体规则（如 `must be a non-empty bearer token`）到 stderr，退出码 `1`，无 traceback；也可为 `asset inspector runtime root is missing:` 或 `asset inspector prlimit is missing:` 加路径 | `async-job-runbook.md` 实测表：退出码 `1` 加同一句 `asset inspector configuration error` 消息 | 是 | 补进程级用例钉住这行（`EXACT_LINE`），并说明它不是 `FAIL:` 前缀 |
| `job-dispatch-worker` · 入口闸门 `deploy/job-startup-gate.py` | 入口脚本 `main()`（`exec` 之前） | 每个缺失项一行 `FAIL:`（含具名条件，stdout 上带两格缩进）加末行汇总，退出码 `1`，失败时不 `exec`；连接失败一行只说 `cannot connect to PostgreSQL` 与异常类型名，不回显驱动原文；schema 非法时一行 `FAIL:` 含 `is not a safe lowercase SQL identifier` | `compose-minimal.md` §6、`async-job-runbook.md` §1/§8/§12：`FAIL:` 点名并以非零退出 | 是 | 补进程级用例钉住；并修两处与承诺相悖的失败面：非法 `SPEECHNEXUS_DATABASE_SCHEMA` 曾是 ValueError traceback（现在点名该键），连接失败行曾回显主机、端口与库名（现在只给异常类型名与人话，因为 psycopg 连接失败时 `sqlstate` 恒为 None）。点名的对象是键名、桶名、schema 与表名，以及该容器自己的私有配置目录路径（容器内路径） |
| `job-dispatch-worker` 自身入口 `speechnexus-job-dispatch-worker` | 入口 `main()`（`JobDispatchSettings` 与依赖装配） | 配置不全时一行 `Job dispatch unavailable; no lease was claimed.` 到 stderr、退出码 `1`；配置通过但依赖装不起来时同一前缀 `Job dispatch unavailable:` 后接 `planner and result publisher are not assembled;` 再以 `no lease was claimed.` 收尾、退出码 `2`；两种情况都无 traceback 且不回显 DSN | 无（文档未承诺该入口的失败输出形态） | 是（无格式承诺） | 改文档：由本行记录这两条具名拒绝与退出码 `1`/`2`，并补进程级用例钉住；不新增 `FAIL:` 承诺（它刻意不用 `FAIL:` 前缀） |
| `artifact-retention`（`speechnexus-artifact-retention`） | 入口 `main()`（`Settings()` 与 `runtime.open()`） | `Traceback …` 后跟 `ValidationError` 或启动拒绝消息，退出码 `1` | `compose-minimal.md` §6 透传表：空值「容器启动即失败」、宽身份「启动点名拒绝」，未承诺形态 | 是（无格式承诺） | 改文档：承认 traceback 与退出码。理由：拒绝消息已点名角色与关系 |
| `workbench-migrate`（`speechnexus-workbench-migrate`） | 入口 `main()` | 每个问题一行 `FAIL:` 到 stderr，退出码 `1`，无 traceback；owner 连接失败为 `the owner connection could not complete the workbench migration` 加异常类型名 | `compose-minimal.md` §6：`FAIL: <具名条件>` 并以非零退出 | 是 | 补进程级用例钉住 |
| `workbench-api` · 入口闸门 `deploy/workbench-startup-gate.py` | 入口脚本（`exec` 之前；`check_database` / `check_cleanup_identity`） | 每个问题一行 `FAIL:`；设置加载被拒为 `the workbench configuration was refused at load` 加异常类型名，运行期检查失败为一行 `FAIL:` 加 `the workbench runtime database was refused at startup:`；退出码 `1`，失败时不 `exec` | `compose-minimal.md` §6：`FAIL:` 点名并以非零退出 | 是 | 补进程级用例钉住 |
| `engine-register`（`speechnexus-register-engine`） | 入口 `main()` | 失败为一行 `error:` 加具名 code（stderr）与对应退出码，成功为一行 JSON | `register-engine.md` 与 `compose-minimal.md` §8：JSON `outcome` 与具名 code，未承诺 `FAIL:` | 是（无格式承诺） | 改文档：承认 `error:` 行与退出码；本单元是一次性命令，不是常驻闸门 |
| 迁移单元 `speechnexus-job-migrate` | 入口 `main()`（alembic `upgrade`） | `Traceback …` 后跟 alembic 或 psycopg 异常，退出码 `1` | `async-job-runbook.md` §5 只写失败时的退出码与如何对账，未承诺输出形态 | 是（无格式承诺） | 改文档：承认 traceback 与退出码。理由：alembic 的异常类型多样，统一成一行会丢掉定位所需的分量 |
| `binding-disable-worker` | 入口 `main()` | 一行 `Disable worker failed; verify its configuration and dependencies`，退出码 `1` | `docs/operations/binding-disable-worker.md` 未承诺失败输出形态 | 是（无格式承诺） | 改文档：承认这行非 `FAIL:` 的消息与退出码 |
| `asset-maintain`（`speechnexus-asset-maintain`） | 入口 `main()`（`Settings()`） | `Traceback …` 后跟 `ValidationError`，退出码 `1` | 无 | 是（无格式承诺） | 改文档：承认 traceback 与退出码 |
| `runtime-delivery-worker`（`speechnexus-runtime-delivery-worker`） | 入口 `main()` | 一行 `Runtime delivery unavailable; original records retained.`，退出码 `1` | `docs/operations/production-runtime-delivery.md` 未承诺失败输出形态 | 是（无格式承诺） | 改文档：承认这行非 `FAIL:` 的消息与退出码 |
| `voice-evidence-worker`（`speechnexus-voice-evidence-worker`） | 入口 `main()` | 一行 `invalid voice evidence worker configuration`，退出码 `2` | 无 | 是（无格式承诺） | 改文档：承认这行非 `FAIL:` 的消息与退出码 `2` |
| `engine-readiness`（`speechnexus-engine-readiness`） | 入口 `main()` | 一行 `error:` 加具名 code（如 `invalid_settings`），退出码 `2` | `register-engine.md` 只描述只读报告，未承诺失败输出形态 | 是（无格式承诺） | 改文档：承认 `error:` 行与退出码 `2` |
| `minio-init`（`deploy/init-object-storage.py`） | 一次性 init 容器的 `main()` | 缺 `MINIO_` 键时 `Traceback …` 后跟 `KeyError`，退出码 `1` | `compose-minimal.md` §4 只写 `bucket ready` 与版本化要求 | 是（无格式承诺） | 改文档：承认 traceback 与退出码，并说明 Compose 渲染期的键守卫先于它拦住缺键，这条 traceback 只在直接运行脚本时可达 |

<!-- startup-gate-output:end -->

## 仍然存在的两处限制（不改，只记录）

1. **`job-startup-gate.py` 的 `write_privilege_findings` 仍回显驱动文本**
   （`str(error)[:300]`）：真实 psycopg 下只带 schema、表名与角色名，没有 DSN，但它属于
   「一行结论」单元里唯一没有用例覆盖的文本来源，后续要收口应另立卡。
2. **`gateway` 的固定 CPU VAD 运行时**（`gateway_vad` 行）需要镜像内的 Silero ONNX 与固定
   依赖，本机无法复现，只按归属记而未实测；同阶段的控制面失败已实测为 traceback + 退出码
   `3`（`gateway_control` 行）。

## 一条命令判断「为什么这个单元起不来」

启动闸门之所以重要，是因为容器日志是唯一可信的现场。按上表先定位阶段，再决定读什么：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml logs --tail=50 <service>
```

- 首行是 `  FAIL:` 或 `FAIL:`：按行内的条件修复，不要先看别的日志；
- 首行是 `asset inspector configuration error:` / `error: <code>:` / `Job dispatch unavailable…`：
  这些单元各有自己的单行格式（见上表），同样是一次结论；
- 出现 `Traceback`：按阶段分两种——同步装配阶段的异常末行就是结论（例如
  `invalid gateway file ASR configuration`、`Invalid private speech admission recipe`），
  而 lifespan 阶段一律以退出码 `3` 结束，结论在 traceback 末行。
