# 参考部署一条命令验收

`scripts/dev-env/reference-acceptance.py` 是参考部署（Compose 最小栈）的验收入口：一次运行按
「工具 → 迁移 → 角色 → 桶 → 栈 → 启动闸门 → 健康」执行，逐步打印**步骤名 / 前置检查 /
预期输出 / 实际结论**，任一步失败即以该步的退出码结束并列出未执行的步骤；结尾再记录各步
PASS/FAIL/未运行、环境（checkout commit、项目名、gate 模式、schema）与服务镜像。它只读取
部署输入、不改 `deploy/`、`docs/operations/compose-minimal.md`、后端或契约文件。容器、卷与
网络按 `--project-name` 命名隔离，可反复运行与清理；但 `deploy/compose.yaml` 固定
`172.28.0.0/24`，第二个参考部署栈在同机会因网络重叠（`Pool overlaps`）起不来，因此「与同机
其它参考栈并行」不成立。`--project-name` 用于同一栈的重跑与清理隔离，不用于并行部署。

## 能证明什么

- 环境文件满足 `deploy/compose.yaml` 的每个 `${KEY:?}` 展开（入口运行时按实际展开计数），并满足
  入口额外要求的 6 个键（见「前置输入」）；三条 Job DSN 指向三个互不相同的角色；
- 数据库迁移收敛到**迁移目录派生的唯一 head**（不硬编码版本号）；
- 三个 Job 角色及其五组授权真实安装到运行时身份上；`roles` 步骤把
  `provision-job-roles.py --print-statements` 渲染的每条 `GRANT` 解析成目录函数检查
  （`has_table_privilege` / `has_column_privilege` / `has_schema_privilege`），与已装 ACL
  逐条做「声明的都在」包含比对，缺任一声明授权即红；重复执行不重建角色、不新增授权；
- 私有输入桶与 Artifact/结果桶存在且已启用版本化；
- API 与 gateway 起来后，`deploy/job-startup-gate.py` 在真实 PostgreSQL、MinIO 与私有配置上
  给出 `OK` 判定；
- API 与 gateway 的 `/healthz` 与 `/readyz` 返回 200。

不证明：真实引擎与已注册 ready 实例、真实 ASR/TTS 业务成功、模型资格、容量或端到端异步 Job。
启动闸门把这些列在 `pending` 行，本入口原样转述，不把它们伪装成 PASS。也不判定角色是否
「非 owner」：本入口只拒绝三条 Job DSN 指向同一身份；首跑时 `provision-job-roles.py` 自身会
拒绝 `superuser`/`createdb`/`createrole` 的身份，幂等分支不重做该校验。

## 前置输入

| 输入 | 说明 |
| --- | --- |
| `deploy/compose.env` | 从 `deploy/compose.env.example` 复制并填写。必需键有两个来源：**compose 派生**——`deploy/compose.yaml` 里每个 `${KEY:?}` 展开键（入口运行时按实际展开计数，当前 14 个；本脚本不另列清单）；**入口额外要求 6 个**——`SN_ADMIN_DSN`、三条 Job DSN、`SPEECHNEXUS_OBJECT_STORAGE_BUCKET`、`SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET`。后 6 个在 compose 里是 `:-` 默认或根本不出现，入口要求显式非空 |
| 三条 Job DSN | `SPEECHNEXUS_JOB_WRITER_DATABASE_URL`、`SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL`、`SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL`，按 §6 生成，身份互不相同 |
| 两个桶名键 | `SPEECHNEXUS_OBJECT_STORAGE_BUCKET`（Artifact/结果桶）、`SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET`（输入桶）；`SPEECHNEXUS_EXECUTION_RESULT_BUCKET` 可省略，省略时等于 Artifact 桶 |
| `SN_ADMIN_DSN` | 迁移 owner 连接串，由 shell 导出（密钥通道），默认不进环境文件 |
| §6 基线角色 | `sn_api`、`SN_GATEWAY_ROLE`（默认 `sn_gateway`）、`sn_gateway_query` 已预置；本入口不重做 §6 的撤权与基线角色 |
| 私有配置目录 | `SPEECHNEXUS_PRIVATE_CONFIG_DIR` 下的 recipe、gateway lane 配置、批准的 Silero ONNX；缺任一项时 `stack` 步点名缺的是哪个文件 |
| 运行时 UID/GID | `SPEECHNEXUS_RUNTIME_UID` / `SPEECHNEXUS_RUNTIME_GID`，取 `id -u` / `id -g` |

## 一条命令

```bash
set -a; . deploy/compose.env; set +a
export SN_ADMIN_DSN="postgresql://$POSTGRES_USER:$POSTGRES_PASSWORD@127.0.0.1:$SPEECHNEXUS_POSTGRES_HOST_PORT/$POSTGRES_DB"

# 1) 只做静态检查并打印计划，不建角色、不建桶、不改库
python3 scripts/dev-env/reference-acceptance.py --dry-run

# 2) 执行并判定（默认 compose 模式即可从零跑，栈在闸门之前）
python3 scripts/dev-env/reference-acceptance.py
```

`--dry-run` 的结论是 `DRY-RUN PASS steps=7`，`pending=N` 表示尚缺的外部输入（例如未提供的
Silero ONNX）；它从不变更数据库、对象存储或角色。执行模式的结论只有两种，机器可判定：

```text
reference acceptance: PASS steps=7
reference acceptance: FAIL step=migrate exit=20
```

执行与 dry-run 都会在结论前打印运行记录：

```text
步骤记录: tools=PASS migrate=PASS roles=PASS buckets=PASS stack=PASS gate=PASS health=PASS
环境记录: repo=/path/to/repo commit=9398627f project=speechnexus mode=apply gate=compose schema=speech
镜像记录: api=speechnexus-api:latest@sha256:…, gateway=speechnexus-gateway:latest@sha256:…
```

失败时 `步骤记录` 标出失败步，并列出 `未运行:` 的后续步骤。

## 步骤、前置检查、预期输出与退出码

| 步骤 | 前置检查 | 预期输出 | 失败退出码 |
| --- | --- | --- | --- |
| `tools` | `docker version` 可用；Compose v2+；python3 ≥ 3.12；`uv` 可用；compose 的 `${KEY:?}` 派生键与入口额外 6 键非空；三条 Job DSN 互异；compose 能渲染 | 版本行；`N 个 compose 派生键 + 6 个入口额外键（共 N+6 个）非空`；`compose 渲染通过` | 10 |
| `migrate` | `alembic.ini` 的 `script_location` 可解析；迁移目录恰好一个 head；`SPEECHNEXUS_MIGRATION_DATABASE_URL` 可用 | `postgres` healthy；`migrate` 用 `--build` 构建镜像后退出 0；`alembic_version` 单行且等于派生 head | 20 |
| `roles` | §6 基线角色存在；三条 Job DSN 身份互异；`SN_ADMIN_DSN` 是迁移 owner | 首跑：`sn_job_writer sn_job_admission sn_job_dispatch 已创建；grants: N statements；M 项声明授权与已装 ACL 逐条核对全部在位；8 个授权探针通过`。重跑：`三个角色已存在（幂等：未重复 CREATE ROLE，未新增授权）；重渲染 N 条 GRANT、M 项授权与已装 ACL 逐条核对全部在位；8 个授权探针通过` | 30 |
| `buckets` | `MINIO_*` 与两个桶名键非空；`minio` 可启动 | 每个不同桶名一行 `bucket ready: ... (versioning enabled)`；独立只读核对通过 | 40 |
| `stack` | 私有配置与批准模型齐备；API/gateway 基线角色已预置 | `docker compose up -d --build --wait api gateway` 退出 0（输出尾行打印），两个服务 `running/healthy`；`up` 非 0 即红，即使旧容器仍是 `running/healthy` | 60 |
| `gate` | `stack` 步已让 api/gateway `running/healthy`；闸门与私有配置可在 `job-dispatch-worker` 内读到 | `job dispatch startup gate: OK`，`ok:` 行给出数据库/对象存储/私有配置结论，`pending:` 行是真引擎与 API 侧 DSN；同时出现 `FAIL:` 行时一律判失败 | 50 |
| `health` | 容器 healthy 且宿主机端口已发布，端口为 1..65535 的整数 | 四个探针 200：API/gateway 的 `/healthz` 为 `status=ok`，`/readyz` 为 `status=ready` | 70 |

参数错误（`--compose-file` 或 `--env-file` 不存在）返回 2。步骤失败以外的未预期异常也按步骤
归口：打印 `FAIL step=<id> exit=<code>` 与输入提示，不抛 traceback、不回退成 Python 默认退出码 1。

顺序说明：**迁移在角色预置之前**（`provision-job-roles.py` 的 `GRANT` 指向 `speech` 下的关系，
关系不存在时脚本在 `relation does not exist` 上失败，与 §5 在 §6 之前一致）；**栈在闸门之前**
（`job-dispatch-worker` 使用 `network_mode: service:gateway`，gateway 未 healthy 时
`docker compose run` 无法启动，闸门也就无法判定）。退出码标识步骤，不表示行序：栈是 60，
闸门是 50，两者相邻且栈在前。

## 机器可读输出（CI 契约）

`.github/workflows/ci.yml` 的 `Deploy: reference acceptance entry (dry-run execution)` 作业解析本入口的日志，
解析与断言的唯一实现在 `scripts/dev-env/ci-entry-guard.py`（子命令 `log`）。下列行是**契约**，不是偶然措辞：
改格式必须同时改本节的表格、守卫脚本与作业断言。

| 行 | 格式 | 约束 |
| --- | --- | --- |
| 首行块 | `steps=<N> order=<csv> mode=<dry-run\|apply> gate=<compose\|direct>` | `<csv>` 的 id 顺序必须等于下面的「步骤顺序」；`<N>` 等于步骤数 |
| 步骤首行 | `[i/N] <ident> — <title>` | `<ident>` 按序对应 `<csv>`，`i` 从 1 开始，分母等于步骤数 |
| 步骤结论 | `      结论: (PLAN\|PASS\|FAIL) — <detail>` | 每步一条；`--dry-run` 下必须全是 `PLAN`，声明 `PASS` 即虚报 |
| 延后项 | `      待输入: <reason>` | 每条都必须登记（见下）；`--dry-run` 下不因延后项判失败，只把它计入 `pending=K` |
| 汇总记录 | `步骤记录: <ident>=<outcome> ...` | 与逐条 `结论:` 一一对应 |
| 结论行 | `reference acceptance: DRY-RUN PASS steps=<N> [pending=<K>]` / `reference acceptance: PASS steps=<N>` / `reference acceptance: FAIL step=<ident> exit=<code>` | `pending=K` 必须等于 `待输入:` 行数；FAIL 行的 `step` 是失败步 id |

**步骤顺序**（`order=` 与 `[i/N]` 必须逐字相符）：
`tools, migrate, roles, buckets, stack, gate, health`。新增或重排步骤时，同步守卫脚本的
`EXPECTED_STEP_ORDER` 与本节——两处不一致会让作业直接红，避免「文档写的顺序」和「实际执行的顺序」
悄悄分叉。

**延后项登记**：`DECLARED_DEFERRALS` 的每条是「slug → 必须同时出现的稳定词」，不匹配整句。把说明写得更
清楚（措辞改变但关键词仍在）不会变红；出现新的延后项、或换用别的关键词，会红并给出登记行与位置。
登记通道是给**无法静态判定**或**新延后输入**用的，不是放宽 PASS 含义的开关。

同一作业还跑另外三条结构守卫：`sites`（每条 `<ctx>.mutate(...)` 必须仍在 `MUTATE_SITES` 清单里、
结果不得被丢弃——裸语句和 `_ =` 都算，赋给 `_` 即红、不看之后有没有再读；`ctx.read(...)` 的 argv 必须是
字面量、`READ_ARGUMENTS` 里登记的构造助手或 `*` 展开的登记只读段，且不含写命令）、`probe`（用必然失败的
输入调用登记过的纯判定函数）、`wiring`（四条守卫子命令各自是所在步骤的第一条被执行的命令，只允许
`set`/`export`/`cd` 之类的环境前置）。它们只检查**结构**：判定条件被反转、把 `.ok` 读出来却仍不中断、
经 `psql -c` 间接写库等等都看不见，边界写在[CI 验证规则](../validation/ci-validation.md)。

登记表与守卫脚本是同一次改动的两面：

- 六张表（`MUTATE_SITES`、`READ_ARGUMENTS`、`WRITE_TOKENS`、`PROBE_CASES`、`DECLARED_DEFERRALS`、
  `SELFTEST_CASES`）的权威定义在 `scripts/dev-env/ci-entry-guard.py`；下面的锚点块是它逐行渲染的
  镜像，每次运行都会比较，缺行、多行、改词都红。
- 改登记的标准动作是改脚本常量，然后跑
  `python3 scripts/dev-env/ci-entry-guard.py tables --write` 重写本块，使两侧在同一次提交里一致。
- 登记不是橡皮章：「判定在哪」列必须用反引号点出入口里真实存在的定义（`step_gate`、`gate_verdict`
  之类），入口改名或删函数即红；「证据」列必须指向一个会被 `unittest` 收集的 `TestCase` 方法或 probe
  用例；类级（含同文件基类）与方法级的 skip/`expectedFailure`、`setUp`/`setUpClass` 里的 `skipTest`、
  同类同名重复定义、开头或字面量恒真分支里的早退、缺失或字面量恒真的断言、会吞 `AssertionError` 的
  `try/except` 都红（逐条形态见
  [CI 验证规则](../validation/ci-validation.md)的 `sites` 段）。校验到的是「这条证据会被收集、会真的
  执行、断言不会被跳过或吞掉」，不是「它覆盖了这个站点的语义」——证据的相关性、`return` 与恒真断言藏在
  非字面量形态里、`*` 展开与构造助手按名字认，以及判定本身，仍由评审和真实 apply 运行负责。
  `SELFTEST_CASES` 是守卫自身的语料，把规则改松会先让自检失败。

<!-- ci-entry-guard:tables:start -->
### 结构守卫的登记表

本块由 `python3 scripts/dev-env/ci-entry-guard.py tables --write` 生成，与 `scripts/dev-env/ci-entry-guard.py` 的常量逐行对应；改动任一登记都必须同时更新另一侧，否则作业红。

#### 行数锚点（任何一张表缩水都要同时改这里的数字）

| 表 | 行数 |
| --- | --- |
| `MUTATE_SITES` | 8 |
| `READ_ARGUMENTS` | 4 |
| `WRITE_TOKENS` | 7 |
| `PROBE_CASES` | 9 |
| `DECLARED_DEFERRALS` | 1 |
| `SELFTEST_CASES` | 20 |

#### MUTATE_SITES（每条写命令必须存在，且能指出谁在别处判定它）

| 站点 | 判定在哪 | 证据 |
| --- | --- | --- |
| `step_migrate:start` | `step_migrate` 的 postgres up 退出码（幂等） | `test:test_behind_migration_stops_before_roles_buckets_and_stack` |
| `step_migrate:migrate` | `migration_verify`：migrate 退出码与 alembic_version 单行比对 | `test:test_migrate_step_builds_before_running` |
| `step_roles:provision_argv()` | `step_roles`：provision-job-roles 退出码 + 已装 ACL 逐条比对 | `test:test_roles_step_creates_once_then_reports_idempotent` |
| `step_buckets:start` | `step_buckets`：minio 启动到 healthy | `test:test_bucket_names_follow_the_init_script_rules` |
| `step_buckets:initialize` | `step_buckets`：minio-init 退出码与建桶输出行数 | `test:test_bucket_names_follow_the_init_script_rules` |
| `step_buckets:verify` | `bucket_names`：独立只读核对桶数量与版本化 | `test:test_bucket_names_follow_the_init_script_rules` |
| `step_gate:argv` | `gate_verdict`：闸门 ok/FAIL 判定 | `probe:gate_verdict-fail` |
| `step_stack:ctx.compose('up', '-d', '--build', '--wait', 'api', 'gateway')` | `step_stack`：compose up 退出码判失败 | `test:test_up_exit_nonzero_with_healthy_ps_is_red` |

#### READ_ARGUMENTS（允许出现在 `read` argv 里的助手、词包装与 `*` 展开段）

| 助手 | 形态 | 为什么只读 |
| --- | --- | --- |
| `psql_argv` | 整体 argv 构造器 | 只读 psql -Atc SELECT/目录查询；写语句不在本函数内构造 |
| `provision_argv` | 整体 argv 构造器 | --print-statements 只读渲染 GRANT 语句 |
| `compose_argv` | 可被 `*` 展开的只读 argv 段 | `docker compose -p … --env-file … -f …` 前缀；展开后仍拼只读子命令 |
| `str` | argv 词包装 | 把仓库路径/端口渲染成单个 argv 词，不产生子命令 |

#### WRITE_TOKENS（`read` 里出现即判红的 argv 词）

`run`, `up`, `start`, `down`, `restart`, `rm`, `create`；`uv run` 是唯一允许的 `run` 配对；`sh`, `bash`, `zsh`, `dash`（及 `python`, `python3`, `perl`, `ruby`, `node`）紧跟 `-c` 一律判红。

#### PROBE_CASES（必须用失败输入调用的纯判定函数）

| 用例 | 入口函数 | 失败形态 | 断言 |
| --- | --- | --- | --- |
| `migration_verify-behind` | `migration_verify` | 返回失败值 | 迁移落后/多行/空 必须失败，正好在 head 通过 |
| `gate_verdict-ok` | `gate_verdict` | raise | OK 行解析出 ok/pending 两类条目 |
| `gate_verdict-fail` | `gate_verdict` | raise | FAIL 行必须抛 StepFailureError |
| `gate_verdict-exit` | `gate_verdict` | raise | 闸门退出码非零不得读成通过 |
| `gate_verdict-fail-wins` | `gate_verdict` | raise | FAIL 与 OK 同时出现必须判失败 |
| `env_gaps` | `env_gaps` | 返回失败值 | 空白键入列表、非空键不入列 |
| `dsn_identity` | `dsn_identity` | raise | 空 DSN 与非 postgresql DSN 必须失败 |
| `migration_heads` | `migration_heads` | raise | 单链取唯一 head，双 head 必须失败 |
| `bucket_names` | `bucket_names` | raise | 去重；三个桶名键全空必须失败 |

#### DECLARED_DEFERRALS（dry-run 允许报告的延后输入）

| slug | 必须同时出现的词 | 出现即视为别的延后项 |
| --- | --- | --- |
| `gateway-requires-running-stack` | `gateway`、`healthy` | `lane`、`配置`、`镜像`、`模型`、`engine`、`引擎` |

#### SELFTEST_CASES（守卫自身的自检语料）

| 用例 | 说明 | 期望 |
| --- | --- | --- |
| `vacuity-empty-module` | 没有 mutate 站点必须红（反空转） | 红 |
| `discarded-result` | 结果被丢弃必须红 | 红 |
| `unregistered-site` | 清单里没有的写命令必须红 | 红 |
| `receiver-agnostic` | 换接收者（ctx.runner.mutate）也算站点，未登记即红 | 红 |
| `truthiness-bare` | 裸对象真值必须红（CommandResult 无 __bool__） | 红 |
| `truthiness-negated` | `if not started:` 必须红（恒真=守卫被架空） | 红 |
| `truthiness-is-not-none` | `is not None` 判定必须红（结果永不为 None） | 红 |
| `truthiness-ok-is-fine` | `.ok` 形态必须绿（结构上不是恒真条件） | 绿 |
| `read-literal-write` | read 里出现 compose down 必须红 | 红 |
| `read-shell-c` | read 里出现 sh -c 必须红 | 红 |
| `read-interpreter-c` | read 里出现解释器 -c 必须红 | 红 |
| `read-uv-run-allowed` | uv run 是唯一允许的 run 配对 | 绿 |
| `read-uv-run-docker-not-exempt` | uv run 不能豁免后面的 compose run | 红 |
| `read-registered-builder` | 登记过的只读 argv 构造器可接受 | 绿 |
| `read-unknown-builder` | 未登记的 argv 构造器必须红 | 红 |
| `read-variable-argv` | read 的 argv 藏在变量里必须红（旧版静默放过） | 红 |
| `read-starred-unregistered` | `*` 展开一个未登记的只读段必须红（旧版静默跳过整个 argv） | 红 |
| `read-starred-registered` | `*` 展开 READ_ARGUMENTS 里登记过的只读 argv 段可接受 | 绿 |
| `discarded-to-underscore` | 把结果赋给 `_` 也是丢弃（旧版只认裸语句） | 红 |
| `registered-site-passes` | 清单内的站点形态必须绿 | 绿 |
<!-- ci-entry-guard:tables:end -->

## 幂等与重跑

- `migrate`：`speechnexus-job-migrate` 在已到 head 的库上是空操作，重复执行不改版本行；每轮
  用 `--build` 构建镜像，避免复用旧镜像得出错误版本结论。
- `roles`：首次运行创建三个角色并安装授权；再次运行时入口先核对角色是否已存在，全部存在则用
  `provision-job-roles.py --print-statements` 重渲染授权，把每条 `GRANT` 解析成目录检查并与
  已装 ACL 逐条比对；任何一条声明授权缺失（包括探针之外的授权，例如对
  `speech.speech_publications` 的 `REVOKE`）都判失败并点名角色/对象/权限。只存在一部分角色时
  直接失败，列出缺失角色，不覆盖已有身份。
- `buckets`：`minio-init` 对已存在的桶只补版本化设置并重新确认；核对步独立读取桶与版本化状态。
- `stack` / `gate` / `health`：`up -d --build --wait` 与只读判定，重复执行结论不变；`gate` 的
  `run --build` 同样避免旧镜像。

## 启动闸门的两种运行方式

`--gate-mode compose`（默认）按部署路径经 `job-dispatch-worker` 服务运行闸门。该服务
`network_mode: service:gateway`，所以 `stack` 步必须先把 `gateway` 起成 `healthy`；`gate` 步仍
保留这条前置，栈没起来时它判失败而不是放行。从零执行默认模式不再在闸门处因「gateway 未启动」
中断——顺序是栈在前、闸门在后，栈失败会以 `stack` 步退出码 60 和容器/构建输出报告真实原因。

`--gate-mode direct` 用同一个镜像、同一份 worker 环境在 Compose 网络上直连运行
`deploy/job-startup-gate.py`，不要求 gateway 容器存在。它验证的是闸门自身全部检查（角色守卫、
同源 SELECT 探针、认领后写入列、迁移 head、两个 Job 桶与版本化、gateway `result_bucket`
一致性）；适合在没有批准模型文件的机器上单独确认授权面与迁移面。

## 常见失败与对应修法

| 现象 | 修法 |
| --- | --- |
| `FAIL — 环境键缺失或为空: KEY` | 在 `deploy/compose.env` 写非空值；三条 Job DSN 用 §6 的脚本从口令占位生成 |
| `FAIL — 三条 Job DSN 必须指向三个互不相同的角色` | 不要复用 API/gateway 角色；按 §6 重新生成三条 DSN |
| `FAIL — 网关基线角色 sn_gateway 不存在` | 先执行 §6 基线角色预置；本入口不替代它 |
| `FAIL — 迁移落后: alembic_version=... head=...` | 跑 `--profile migration run --rm migrate`；不要手工改版本表 |
| `FAIL — 迁移脚本有 2 个 head` | 先合并迁移分支，单库只能有一个 head |
| `FAIL — provision-job-roles.py 失败: ... relation does not exist` | 迁移尚未应用；先跑 `migrate` 步骤 |
| `FAIL — 已装 ACL 与声明的 N 条 GRANT 不一致: … 缺少 …` | 按 §6 重新安装点名的 grant 组；不要只补 8 个探针 |
| `FAIL — 无法解析的授权语句` | `provision-job-roles.py` 渲染出了本入口不认识的 `GRANT` 形态；同步本入口的解析器，不要当作已安装 |
| `FAIL — 桶核对失败` | 检查 MinIO root 凭据与 `minio-init` 输出，版本化不能被删除后继续 |
| `FAIL — 栈未起来（docker compose up 退出码 N）` | 看错误信息里的构建输出；旧容器的 `running/healthy` 不代表新一轮构建成功 |
| `FAIL — 栈未 healthy: api=...` | 按 §9 失败对照表看容器日志（recipe 摘要、DSN、对象存储健康 URL） |
| `FAIL — gateway 未 healthy` | 看 `stack` 步失败原因（私有配置、模型、容器日志）；单独执行 `gate` 时先跑 `stack` 或改用 `--gate-mode direct` |
| `FAIL — 启动闸门判定失败: bucket ... does not exist` | 按闸门点名补桶或版本化，再重跑 `buckets` |
| `FAIL — 缺少 gateway lane 配置 / vad_model_file 不存在 / 摘要不符` | 按 §3 复制并核对私有配置、模型摘要与 `objects.result_bucket` |
| `FAIL — 基线运行角色缺失: sn_api, ...` | 按 §6 预置基线角色后再启动栈 |
| `FAIL — 未预期异常 …` | 核对本步的环境键取值、端口（`SPEECHNEXUS_API_HOST_PORT` 等必须是整数端口）与文件路径后重跑 |

## 与其它入口的关系

- `scripts/dev-env/check-async-job-prereqs.py`：只读六项前置检查，输出 PASS/FAIL/PENDING，
  可在没有 Docker 时跑静态部分；用于诊断，不做任何修复。
- `scripts/dev-env/provision-job-roles.py`：角色与授权的唯一来源；本入口调用它、渲染其语句并
  在已装 ACL 上核对，不复制授权清单。
- `deploy/job-startup-gate.py`：容器启动闸门；本入口只运行它并转述判定，不复制其检查逻辑。
- `docs/operations/compose-minimal.md`：从零部署的完整步骤；本入口是其在验收方向的收口。
