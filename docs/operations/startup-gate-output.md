# 启动闸门的失败输出形态

每一处「拒绝启动」的判定都只有两种可维护的形态：在**同步装配**或**入口脚本**里先说结论
（一行具名 `FAIL:` 或一行具名消息，然后非零退出），或者把它留在 **ASGI lifespan** 里由
uvicorn 结束进程（traceback + 退出码 `3`）。两种都可以是对的，含糊才是错的：运维只看
容器日志，无法从「重启循环」里推断该改哪个键、该跑哪条命令。

本文是这张判定的唯一表格：**单元 × 闸门所在阶段 × 真实输出形态 × 文档承诺 × 是否一致 ×
处置**。每一行都是 `2026-11-17` 在本机 worktree（`origin/main` `c33891ab`）上用真实入口实测
的，不是从代码推断的；唯一的例外在「未验证」列里点名。启动一个真实入口与断言输出形态的
用例在 `tests/unit/backend/test_startup_gate_process_output.py`（无需数据库）与
`tests/integration/test_startup_gate_output_pg.py`（需要隔离 PostgreSQL）。

`tests/ci/startup_gates.py` 是这张表的唯一来源：`tests/ci/test_startup_gate_output_map.py`
从它渲染表格并逐字比对下文，改一侧不改另一侧即红；同一处还核对每个「承诺一行 `FAIL:`」的
行都点名了一个真的启动过入口的用例。

## 读法

- **同步装配**：uvicorn `config.load()` 调用 ASGI factory 的阶段。异常从 factory 抛出，穿过
  `uvicorn.run()` 回到入口的 `try`，因此入口可以打印结论并 `SystemExit(1)`。
- **ASGI lifespan**：`Server.startup()` 里 `await self.lifespan.startup()` 之后
  `if self.lifespan.should_exit: sys.exit(3)`（uvicorn 0.54.0）。这一步 `sys.exit(3)` 是
  `BaseException`，入口的 `except <具体配置异常>` 到不了这里，所以输出必然带 traceback，
  退出码是 `3` 而不是 `1`。
- **入口 `main()`**：进程自己在进入任何服务循环之前的校验。它能不能给出一行结论，取决于它
  自己有没有包住那段校验。
- **不泄露**：给出一行结论的单元不得在输出里带 DSN 片段、主机、库名、用户名；键名、桶名、
  schema 与表名属于部署面，可以点名（本文逐行记录了当前确实点名了哪些对象）。

## 表格

<!-- startup-gate-output:start -->

| 单元 | 阶段 | 真实输出（实测） | 文档承诺 | 一致 | 处置 |
| --- | --- | --- | --- | --- | --- |
| `api`（`speechnexus-api`）· Job writer 迁移与角色闸门 | ASGI lifespan（`app.py` 的 `await job_runtime.open()`） | `ERROR: Traceback ...`（经 starlette lifespan 到 `job_runtime.open`）+ 退出码 `3`，没有任何 `FAIL:` 行 | `async-job-runbook.md` §1 行 2 与 `#924` 的 §9.1 承诺一行 `FAIL: … migration profile`且「不再 traceback」 | 否 | 由 `#924` 把文档改成实测为真（本卡不改 `app.py` / `main.py` / `job_runtime.py`）。若要把该预检移到入口换一行 `FAIL:`，须另立卡：它涉及启动顺序、二次连接与 lifespan 归属 |
| `api` · 同步 TTS 结算预检 | 同步装配（`speech_runtime.build_speech`） | 一行 `FAIL: the settled-capacity preflight cannot read the admission tables …`，退出码 `1`，无 traceback | `async-job-runbook.md` §8 表：日志首行 `FAIL: the settled-capacity preflight …`、退出码 `1` | 是 | 补进程级用例钉住（`tests/integration`，需要隔离 PostgreSQL 的空 schema） |
| `api` · 私有配方加载（TTS `speech-recipe` 与转写 `transcription-recipe` 两个 loader） | 同步装配（uvicorn factory 内 `load_tts_recipe` / `load_transcription_recipe`） | 实测（TTS 配方）：`Traceback … ValueError: Invalid private speech admission recipe <abs path> (<Type>)`，退出码 `1`；转写 loader 用同一包装（`transcription_recipe.py:272`）抛 `Invalid private transcription admission recipe`，未单独起进程复现（结算预检先于它拒绝空 schema） | `compose-minimal.md` §9 与 `async-job-runbook.md` §5：recipe 摘要不符时「API 启动报 recipe digest / `invalid transcription recipe`」，未承诺输出形态与退出码 | 是（无格式承诺） | 改文档：在本表承认 traceback 与退出码。理由：这条失败来自部署方手改的私有文件，消息已点名文件与失败类；包一层 `FAIL:` 只会重复同一句，且会把「配方不可用」与「库未迁移」两类混淆 |
| `api` · 设置校验 | 入口 `main()`（`Settings()`） | `Traceback … pydantic ValidationError`，退出码 `1` | `compose-minimal.md`：某些键非法时「stops API startup / 启动失败」，未承诺输出形态 | 是（无格式承诺） | 改文档：在本表承认 traceback 与退出码。理由：pydantic 已在消息里点名键与规则，且这段在任何 uvicorn 装配之前运行，`main()` 无法在不吞掉真实缺陷的前提下统一改写 |
| `api` · 其余 lifespan 运行时（binding list / catalogue / audit 等） | ASGI lifespan（`run_in_threadpool(<runtime>.open)`） | `ERROR: Traceback …`（`<runtime> startup rejected`）+ 退出码 `3`，无 `FAIL:` 行 | 无（文档只描述这些身份与它所覆盖的读面） | 是（无格式承诺） | 改文档：承认 lifespan 阶段一律 traceback + 退出码 `3`。理由：uvicorn 0.54.0 的`Server.startup()` 在 lifespan 失败时 `sys.exit(3)`，入口的 `except` 到不了这个 raise 点 |
| `execution-gateway`（`speechnexus-gateway`）· 设置校验 | 入口 `main()`（`Settings()`） | `Traceback … pydantic ValidationError`，退出码 `1` | `compose-minimal.md` §9 只点名原因文案，未承诺输出形态 | 是（无格式承诺） | 改文档：承认 traceback 与退出码。理由：与 `api` 设置校验同构，且这是 uvicorn 之前的配置拒绝，没有第二个可执行阶段可挪 |
| `execution-gateway` · 文件语音配置（ASR/TTS/VoiceDesign/Clone 四 lane） | 同步装配（factory `create_app` 的 `load_file_speech_configuration`） | `Traceback … ValueError: invalid gateway file ASR configuration`，退出码 `1` | `compose-minimal.md` §9：`gateway 退出并报 invalid gateway file ASR configuration`，未承诺输出形态 | 是（无格式承诺） | 改文档：承认 traceback 与退出码，并说明原因文案出现在 traceback 末行。理由：这条拒绝发生在同步装配阶段，`main()` 外没有第二个入口；把它改写成一行 `FAIL:` 会新增一条需要单独维护的承诺，而当前文档只承诺点名原因 |
| `execution-gateway` · 固定 CPU VAD 运行时与控制面 | ASGI lifespan（`_build_control` / `runtime.open`） | 未验证：需要镜像内的 Silero ONNX 与固定依赖，本机 worktree 无法复现 | `compose-minimal.md` §9：`gateway 报 cannot initialize the pinned CPU VAD runtime` | 未验证 | 记录为未验证项：按 lifespan 归属推断为 traceback + 退出码 `3`，但未实测；不得据此写成实测结论 |
| `node-controller`（`speechnexus-node-controller`，默认 Compose 不启动） | 入口 `main()`（`Settings()`）与 factory | `Traceback … pydantic ValidationError`（设置）/ factory 拒绝，退出码 `1` | `docs/components/node-controller.md`：启动即校验配置，未承诺输出形态 | 是（无格式承诺） | 改文档：承认 traceback 与退出码。理由：本单元不在默认 Compose 内，没有部署文档承诺失败形态；先记录实测，不新增约定 |
| `asset-inspector`（`speechnexus-asset-inspector`） | 入口 `main()`（`InspectionSettings.from_env` 与运行时路径检查） | 一行 `asset inspector configuration error: <key rule>`（stderr），退出码 `1`，无 traceback；缺 runtime root 时一行 `asset inspector runtime root is missing: <path>` | `async-job-runbook.md` 实测表：退出码 `1` 加同一句 `asset inspector configuration error …` | 是 | 补进程级用例钉住这行（`EXACT_LINE`），并说明它不是 `FAIL:` 前缀 |
| `job-dispatch-worker` · 入口闸门 `deploy/job-startup-gate.py` | 入口脚本 `main()`（`exec` 之前） | 每个缺失项一行 `  FAIL: <具名条件>`（stdout）+ 末行汇总，退出码 `1`，失败时不 `exec` | `compose-minimal.md` §6、`async-job-runbook.md` §1/§8/§12：`FAIL:` 点名并以非零退出 | 是 | 补进程级用例钉住；并修两处与承诺相悖的失败面：非法 `SPEECHNEXUS_DATABASE_SCHEMA` 曾是 ValueError traceback，连接失败行曾回显主机、端口与库名。点名的对象是键名、桶名、schema 与表名，以及该容器自己的私有配置目录路径（容器内路径，不是构建机路径） |
| `artifact-retention`（`speechnexus-artifact-retention`） | 入口 `main()`（`Settings()` 与 `runtime.open()`） | `Traceback … ValidationError` 或启动拒绝消息，退出码 `1` | `compose-minimal.md` §6 透传表：空值「容器启动即失败」、宽身份「启动点名拒绝」，未承诺形态 | 是（无格式承诺） | 改文档：承认 traceback 与退出码。理由：拒绝消息本身已点名角色与关系；把它改写成`FAIL:` 会与本次未统一的其余 worker 形成第三种约定 |
| `workbench-migrate`（`speechnexus-workbench-migrate`） | 入口 `main()` | 每个问题一行 `FAIL: <具名条件>`（stderr），退出码 `1`，无 traceback | `compose-minimal.md` §6：`FAIL: <具名条件>` 并以非零退出 | 是 | 补进程级用例钉住 |
| `workbench-api` · 入口闸门 `deploy/workbench-startup-gate.py` | 入口脚本（`exec` 之前；`check_database` / `check_cleanup_identity`） | 每个问题一行 `FAIL:`；运行期检查失败为一行 `FAIL: the workbench runtime database …`，退出码 `1`，失败时不 `exec` | `compose-minimal.md` §6：`FAIL:` 点名并以非零退出 | 是 | 补进程级用例钉住 |
| `engine-register`（`speechnexus-register-engine`） | 入口 `main()` | 失败为一行 `error: <code>: <message>`（stderr）+ 退出码（参数错 `2`、不可达等具名 code），成功为一行 JSON | `register-engine.md` 与 `compose-minimal.md` §8：JSON `outcome` 与具名 code，未承诺 `FAIL:` | 是（无格式承诺） | 改文档：承认 `error: <code>` 行与退出码；本单元是一次性命令，不是常驻启动闸门 |
| 迁移单元 `speechnexus-job-migrate` | 入口 `main()`（alembic `upgrade`） | `Traceback …`（alembic / psycopg 异常），退出码 `1` | `async-job-runbook.md` §5 只写失败时的退出码与如何对账，未承诺输出形态 | 是（无格式承诺） | 改文档：承认 traceback 与退出码。理由：alembic 的异常类型多样（连接、版本冲突、SQL 失败），统一成一行会丢掉定位所需的分量 |
| `binding-disable-worker` | 入口 `main()` | 一行 `Disable worker failed; verify its configuration and dependencies`，退出码 `1` | `docs/operations/binding-disable-worker.md` 未承诺失败输出形态 | 是（无格式承诺） | 改文档：承认这行非 `FAIL:` 的消息与退出码；不新增约定 |
| `asset-maintain`（`speechnexus-asset-maintain`） | 入口 `main()`（`Settings()`） | `Traceback … ValidationError`，退出码 `1` | 无 | 是（无格式承诺） | 改文档：承认 traceback 与退出码 |
| `runtime-delivery-worker`（`speechnexus-runtime-delivery-worker`） | 入口 `main()` | 一行 `Runtime delivery unavailable; original records retained.`，退出码 `1` | `docs/operations/production-runtime-delivery.md` 未承诺失败输出形态 | 是（无格式承诺） | 改文档：承认这行非 `FAIL:` 的消息与退出码 |
| `voice-evidence-worker`（`speechnexus-voice-evidence-worker`） | 入口 `main()` | 一行 `invalid voice evidence worker configuration`，退出码 `2` | 无 | 是（无格式承诺） | 改文档：承认这行非 `FAIL:` 的消息与退出码 `2` |
| `engine-readiness`（`speechnexus-engine-readiness`） | 入口 `main()` | 一行 `error: invalid_settings: <what to set>`，退出码 `2` | `register-engine.md` 只描述只读报告，未承诺失败输出形态 | 是（无格式承诺） | 改文档：承认 `error: <code>` 行与退出码 `2` |
| `minio-init`（`deploy/init-object-storage.py`） | 一次性 init 容器的 `main()` | 缺 `MINIO_*` 时 `Traceback … KeyError`，退出码 `1`；桶/版本化不符时抛异常退出 | `compose-minimal.md` §4 只写 `bucket ready` 与版本化要求 | 是（无格式承诺） | 改文档：承认 traceback 与退出码，并说明 Compose 渲染期的 `:?` 守卫先于它拦住缺键，这条 traceback 只在直接运行脚本时可达 |

<!-- startup-gate-output:end -->

## 两处与本表不一致的地方

1. **`api` 的 Job writer 迁移闸门**（`api_job_schema` 行）跑在 ASGI lifespan 里，实测是
   traceback + 退出码 `3`，而 `async-job-runbook.md` §1 与 `compose-minimal.md` 的 §9.1
   承诺一行 `FAIL: … migration profile`。这条承诺由 `#924` 收口：它把自己的文档改成实测为真，
   并说明为什么这条只能是 traceback（预检在 lifespan 里，入口的 `except` 不可达）。本卡不改
   `backend/src/speechnexus_backend/{app,main}.py` 与 `job_runtime.py`。
2. **`gateway` 的固定 VAD 运行时**（`gateway_vad_lifespan` 行）需要镜像内的 Silero ONNX 与
   固定依赖，本机无法复现，因此只按归属记录、不作实测结论。

## 一条命令判断「为什么这个单元起不来」

启动闸门之所以重要，是因为容器日志是唯一可信的现场。按上表先定位阶段，再决定读什么：

```bash
docker compose --env-file deploy/compose.env -f deploy/compose.yaml logs --tail=50 <service>
```

- 首行是 `  FAIL:` 或 `FAIL:`：按行内的条件修复，不要先看别的日志；
- 首行是 `asset inspector configuration error:` / `error: <code>:` / `Disable worker failed`：
  这些单元各有自己的单行格式（见上表），同样是一次结论；
- 出现 `Traceback`：按阶段分两种——同步装配阶段的异常末行就是结论（例如
  `invalid gateway file ASR configuration`、`Invalid private speech admission recipe`），
  而 lifespan 阶段一律以退出码 `3` 结束，结论在 traceback 末行。
