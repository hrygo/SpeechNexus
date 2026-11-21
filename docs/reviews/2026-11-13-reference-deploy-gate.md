# 参考部署启动闸门的非作者审查（#642 / #572）

- **审查对象**：`8404efad`（`fix: 参考部署补齐异步 Job 前置条件 (#572) (#642)`），父提交 `3b6b1f96`，已合入 `origin/main`。
- **基线**：`origin/main` head `6d26265a`。事实一律取自 `git show origin/main:...`（主检出停在 `docs/tracker-batch12`，未读其工作树）。
- **审查分支**：`review/655-reference-deploy`，独立 worktree `/private/tmp/sn655`（`git worktree add /private/tmp/sn655 -b review/655-reference-deploy origin/main`）。
- **审查者**：非作者。只读代码、契约、测试、文档与提交/PR 文字；结论由本机实测得出。
- **审查日期**：2026-11-13。
- **审查方式**：静态审查 + 真的构建一次 Python 镜像 + 隔离 PostgreSQL 16.15 / MinIO（均来自 `deploy/` 配置）上实跑闸门 + 8 组测试级变异 + 12 组运行时变异。**未改动产品代码**；所有临时改动用 `git checkout --` 逐个还原，实验后 `git status --porcelain` 为 0，`uv.lock` 未被改动；隔离容器、网络、镜像与临时目录在收尾时删除。

> 两个时点必须分开看，否则会把后续提交的漂移算到本片头上：
> - **`8404efad` 当时**：迁移 head `0052_quarantine_releases`，`grants: 220 statements`，`tests/ci` 91 项。本片文档里的 `0052` 与 `220` 在此刻是**对的**。
> - **`origin/main` 现在**（`6d26265a`）：head `0053_voice_draft_publication`，`grants: 221 statements`，`tests/ci` 108 项。差异来自后续提交（`voice_draft_schema.py` 增 1 条授权、新增 `0053` 迁移、`compose-minimal.md` 的 head 被后续提交从 `0052` 改成 `0053`）。本报告引用的闸门文件在 `8404efad` 与 `origin/main` 之间**逐字节相同**，所以运行时结论对两者都成立。

---

## 结论

**有条件通过。**

核心承诺是真的：我在隔离 PG + MinIO 上把六项前置按参考部署填齐后，闸门打印 `job dispatch startup gate: OK` 并以 UID 10001 `exec` 到 worker 命令；缺桶、桶未版本化、缺 SELECT 探针、缺写列、gateway `result_bucket` 不一致、角色持 `CREATE`、缺 `voice_validation` 授权，全部以退出码 1 点名阻断，且 `exec` 未发生（见[运行时变异](#运行时变异) A–K）。镜像补齐 `asset-inspector` 后确实能构建，闸门在镜像内可导入、能解析出包内迁移 head。

但有三个**阻塞**问题，都在「声明了却没被真正守住」这条线上：

1. **迁移 head 的 `pending` 分支对真实落后的库放行**（实测 `exit 0` + `exec`）。作者把它声明为取舍，我证明的不是「理论可能」：库停在 `0052`、制品 head 是 `0053`、派发角色没有只读 `SELECT`（正是本片之前所有已建角色的状态），闸门输出 `pending: migration head not verifiable` 后照常启动。见 [B1](#b1-迁移-head-在缺只读授权时对真实落后的库放行)。
2. **闸门的「认领后写入面」少一张表**：撤销 `speech_artifacts` 的 `INSERT`（worker 在结果发布时必写）后，闸门仍 `OK` + `exec`，而这正是它声称消灭的「claim 之后才炸」。见 [B2](#b2-写入面缺-speech_artifacts-insert媒体发布必写)。
3. **`main()` 的装配与退出码零守护**：把 FAIL 分支的 `return 1` 改成 `return 0`、把迁移检查/写权限检查/桶收集从 `main()` 里摘掉，`37 passed` 全绿。作者声明「删掉 result 桶会变红」可复现，但「少一组 grant」只在整个组名从脚本里消失时才变红，单角色少一组仍绿。见 [B3](#b3-main-的装配退出码与反例声明没有守护)。

另有 12 条不阻塞问题（诊断性、文档自相矛盾、断言名不副实），见[缺陷清单](#缺陷清单)。

---

## 时间线

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 | `git worktree add /private/tmp/sn655 -b review/655-reference-deploy origin/main` | worktree 在 `6d26265a`，与主工作区、`sn653` 隔离 |
| T1 | `git show 8404efad --stat`；逐文件比对 `8404efad` 与 `origin/main` | 闸门/provision/两套测试/`init-object-storage.py`/Dockerfile 逐字节相同；`compose.yaml`、`compose-minimal.md` 有后续漂移（与本片无关的字段） |
| T2 | `docker build -f deploy/images/python.Dockerfile -t sn-review-python:655 .` | **BUILD_EXIT=0**；`asset-inspector/src` 进入 venv |
| T3 | 容器内导入闸门模块 | `expected head = 0053_voice_draft_publication`（`origin/main`）/`0052`（`8404efad`）；写入面 20 列；探针 8+2 条 |
| T4 | 起隔离 `postgres:16.15-alpine` + 仓库 `speechnexus-minio`，跑 `speechnexus-job-migrate` | head `0053`；用真实 `init-object-storage.py` 建并版本化 3 个桶 |
| T5 | `provision-job-roles.py`（三条 Job DSN + `SN_GATEWAY_ROLE=sn_gateway`） | `grants: 221 statements`（`origin/main`）；同一脚本在 `8404efad` 树上是 **220** |
| T6 | 运行时变异 A–M（详见下表） | 9 组 fail-closed 正确；2 组应点名却放行/崩栈 |
| T7 | 测试级变异 M1–M8 | 3 组红、5 组绿（含退出码与装配） |
| T8 | `python3 -m unittest discover -s tests/ci`、`ruff check/format` | 108 项 OK（`8404efad` 树为 91 项，与作者记录一致）；ruff 全过 |
| T9 | 清理容器/镜像/临时目录；写报告、提交、推分支、开 PR | 见文末 |

---

## 环境与复现命令

```bash
git worktree add /private/tmp/sn655 -b review/655-reference-deploy origin/main
cd /private/tmp/sn655

# 1) 真的构建一次参考 Python 镜像（context 是仓库根，与 compose 的 build 锚点一致）
docker build -f deploy/images/python.Dockerfile -t sn-review-python:655 .

# 2) 镜像内导入闸门（闸门不在镜像里，按 compose 的方式挂载；compose 就是这么做的）
docker run --rm -v $PWD/deploy/job-startup-gate.py:/opt/speechnexus/job-startup-gate.py:ro \
  --entrypoint python sn-review-python:655 -c "
import sys, importlib.util
spec = importlib.util.spec_from_file_location('g','/opt/speechnexus/job-startup-gate.py')
m = importlib.util.module_from_spec(spec); sys.modules['g']=m; spec.loader.exec_module(m)
print(m.expected_migration_head(), sum(len(c) for _,_,c in m.DISPATCH_WRITE_SURFACE))"

# 3) 隔离拓扑：PG 16.15 + 仓库 minio 镜像（不是公网 minio 镜像）
docker network create sn655-net
docker run -d --name sn655-pg --network sn655-net -e POSTGRES_DB=speechnexus \
  -e POSTGRES_USER=speechnexus -e POSTGRES_PASSWORD=ownerpw postgres:16.15-alpine
docker run -d --name sn655-minio --network sn655-net -e MINIO_ROOT_USER=speechnexus \
  -e MINIO_ROOT_PASSWORD=minio-secret-123 --entrypoint "" \
  speechnexus-minio:RELEASE.2025-09-07T16-13-09Z /usr/local/bin/minio server /data

# 4) 迁移（镜像内的真实入口）
docker run --rm --network sn655-net \
  -e SPEECHNEXUS_MIGRATION_DATABASE_URL='postgresql://speechnexus:ownerpw@sn655-pg:5432/speechnexus' \
  sn-review-python:655 speechnexus-job-migrate --schema speech

# 5) 桶初始化（脚本从 deploy/ 挂载，与 compose 的 minio-init 同一份）
docker run --rm --network sn655-net -v $PWD/deploy/init-object-storage.py:/opt/init.py:ro \
  -e MINIO_ENDPOINT=http://sn655-minio:9000 -e MINIO_ACCESS_KEY=speechnexus \
  -e MINIO_SECRET_KEY=minio-secret-123 \
  -e SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET=speechnexus-inputs \
  -e SPEECHNEXUS_OBJECT_STORAGE_BUCKET=speechnexus-results \
  -e SPEECHNEXUS_EXECUTION_RESULT_BUCKET=speechnexus-staging \
  --entrypoint python sn-review-python:655 /opt/init.py

# 6) 三个 Job 角色（先建基线 gateway 角色，脚本会拒绝缺失）
docker exec sn655-pg psql -U speechnexus -d speechnexus -c \
  "CREATE ROLE sn_gateway LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD 'gwpw'"
docker run --rm --network sn655-net -v $PWD/scripts/dev-env/provision-job-roles.py:/opt/prov.py:ro \
  -e SPEECHNEXUS_JOB_WRITER_DATABASE_URL='postgresql://sn_job_writer:writerpw@sn655-pg:5432/speechnexus' \
  -e SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL='postgresql://sn_job_admission:admissionpw@sn655-pg:5432/speechnexus' \
  -e SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL='postgresql://sn_job_dispatch:dispatchpw@sn655-pg:5432/speechnexus' \
  -e SN_ADMIN_DSN='postgresql://speechnexus:ownerpw@sn655-pg:5432/speechnexus' \
  --entrypoint python sn-review-python:655 /opt/prov.py

# 7) 闸门（entrypoint 形态与 compose 完全一致：脚本 + argv[1:] 即 worker 命令）
GATE=(--rm --network sn655-net
  -v $PWD/deploy/job-startup-gate.py:/opt/speechnexus/job-startup-gate.py:ro
  -v /private/tmp/sn655-private:/run/speechnexus-private:ro
  -e SPEECHNEXUS_DATABASE_URL=postgresql://sn_job_dispatch:dispatchpw@sn655-pg:5432/speechnexus
  -e SPEECHNEXUS_DATABASE_SCHEMA=speech -e SPEECHNEXUS_JOB_DISPATCH_KINDS=asr,tts
  -e SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT=http://sn655-minio:9000
  -e SPEECHNEXUS_OBJECT_STORAGE_BUCKET=speechnexus-results
  -e SPEECHNEXUS_EXECUTION_RESULT_BUCKET=speechnexus-staging
  -e SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY=speechnexus
  -e SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY=minio-secret-123
  -e SPEECHNEXUS_JOB_GATE_PRIVATE_DIR=/run/speechnexus-private
  --entrypoint python sn-review-python:655 /opt/speechnexus/job-startup-gate.py)
docker run "${GATE[@]}" sh -c 'echo WORKER-EXECED as $(id -u)'
```

测试在仓库既有 venv 上跑、`PYTHONPATH` 指向 worktree 源码，避免为主检出或 `uv.lock` 引入改动：

```bash
export PYTHONPATH=/private/tmp/sn655/backend/src:/private/tmp/sn655/packages/contracts/src
/Users/hrygo/Documents/SpeechNexus/.venv/bin/python -m pytest \
  tests/ci/test_reference_deploy_job_prereqs.py tests/unit/test_job_startup_gate.py -q   # 37 passed
```

---

## 逐条清单结论

### 1. 闸门覆盖面与顺序：所有判定都在 `exec` 之前，但「六项」只有三项硬失败

闸门的结构是**先跑完所有检查、再统一打印、最后才 `exec`**：`main()` 里三次独立检查（`database_findings` → `referenced_buckets`/`object_storage_client`/`bucket_findings` → `gateway_config_findings`）把 findings 汇总，`if failures:` 打印后 `return 1`，只有 `failures` 为空才 `os.execvp`（`deploy/job-startup-gate.py:488-548`）。`database_findings` 内部则按「角色守卫 → SELECT 探针 → 写列 → 迁移 head」短路（`:454-468`），并在任一失败时不再继续后面的数据库检查。**没有任何一个出口在打印 FAIL 之后还 `exec`。** 逐出口清单：

| # | 缺失项 | 行为 | 退出码 | 是否 exec | 证据 |
| --- | --- | --- | --- | --- | --- |
| 1 | 包内迁移脚本不可解析（`expected_migration_head()` 抛错） | 未捕获异常 | 1 | 否 | 代码路径 `:491`，**未实测** |
| 2 | 派发 DSN 空/不可解析 | `FAIL: ... is blank` / `not a usable postgresql:// DSN` | 1 | 否 | 单测 `:96-104` |
| 3 | 连不上 PG（网络/角色/密码） | `FAIL: ... cannot connect` | 1 | 否 | 代码 `:432-446` |
| 4 | 角色是 superuser / 持 schema `CREATE` / 库 owner / 表 owner 成员 | `FAIL: dispatch role ... ` | 1 | 否 | **实测 H** |
| 5 | worker 同源 SELECT 探针失败（第一条即停） | `FAIL: cannot run the startup probe on <表>` | 1 | 否 | **实测 C**；单测 `:259-268` |
| 6 | `voice_validation` 车道缺两张 `SELECT` | 同上 + `voice_validation also needs ...` | 1 | 否 | **实测 I** |
| 7 | 20 个后置写入列缺授权 | 逐列 `FAIL: lacks <表>.<列> (UPDATE/INSERT)` | 1 | 否 | **实测 B** |
| 8 | **上述列在库里根本不存在**（迁移落后） | `psycopg.errors.UndefinedColumn` **未捕获崩溃** | 1 | 否 | **实测 L**，见 [S1](#s1-缺写列时崩栈而不是点名-fail) |
| 9 | `speech.alembic_version` 关系缺失 | `FAIL: ... is missing: run the migration profile` | 1 | 否 | 单测 `:288-295` |
| 10 | 版本表 0 行或多行 | `FAIL: holds N versions` | 1 | 否 | 单测 `:329-336` |
| 11 | head 落后 | `FAIL: migration is behind: ... is X, the artifact head is Y` | 1 | 否 | **实测 D1** |
| 12 | **读不到 head（缺只读 SELECT）** | `pending: migration head not verifiable` | **0** | **是** | **实测 D2** → [B1](#b1-迁移-head-在缺只读授权时对真实落后的库放行) |
| 13 | Artifact/result 桶 env 空 | `FAIL: <env> is blank` | 1 | 否 | 单测 `:121-126` |
| 14 | 对象存储 endpoint/凭据缺失或非法 | `FAIL: <keys> blank` / `not an http(s) origin` | 1 | 否 | 代码 `:392-419` |
| 15 | 对象存储不可达 | `FAIL: object storage is unreachable for <env>=<bucket>` | 1 | 否 | 单测 `:153-158` |
| 16 | 桶不存在 | `FAIL: bucket <b> (<env>) does not exist` | 1 | 否 | **实测 E** |
| 17 | 桶存在但未版本化 | `FAIL: bucket <b> (<env>) is not versioned` | 1 | 否 | **实测 F** |
| 18 | gateway 私有配置缺失/无 `objects.result_bucket` | `FAIL: <file> is missing` / `has no readable objects.result_bucket` | 1 | 否 | 单测 `:181-184` |
| 19 | 配置 `result_bucket` 与 env 不一致 | `FAIL: ... make them identical` | 1 | 否 | **实测 G** |
| 20 | 车道不在 `asr/tts`（含 `voice_design`/`voice_clone`/任意串） | `pending: gateway lane ... is not assembled` | **0** | **是** | **实测 J/K** → [S2](#s2-本参考部署无法服务的车道记为-pending-并启动) |
| 21 | 真实引擎/ready 实例 | `pending: ... deployment input this gate cannot verify` | 0 | 是 | 设计如此（声明） |
| 22 | API 侧 writer/admission DSN | `pending: ... live in the api container` | 0 | 是 | 设计如此（声明） |
| 23 | 全部通过 | `job dispatch startup gate: OK` | 0 | 是 | **实测 A** |
| 24 | `execvp` 目标不存在 | `OSError` 未捕获 | 1 | — | 代码路径，未实测 |

对照 `docs/operations/async-job-runbook.md:25-34` 的六项：闸门**硬失败**覆盖 #2（条件见 B1）、#3、#5；#1、#4 只打印 `pending`；#6（`network_mode: service:gateway` 的启动顺序与网关重建）闸门一个字都没检查——它从不读 `SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL`，也不探 gateway。这一项由 compose 的 `depends_on: gateway: service_healthy` 与 `depends_on.restart: true` 承担，runbook §9 自己也这么写。所以本片文件头「It verifies every deployment prerequisite that the dispatcher process cannot discover until after it has claimed a lease」（`:1-9`）与 `deploy/README.md:196-197` 的「verifies the prerequisites」需要读成「verifies the subset it can observe」；#1/#4 的降级是有理由的（创建侧先失败），#6 不在闸门职责内，但**文档没有把这层边界写清楚**，见 [S4](#s4-六项前置的覆盖边界没有写在闸门契约里)。

### 2. fail-closed 的真假：`FAIL` 面是硬的，`pending` 面有一条真开口

`pending` 与 `FAIL` 的边界在代码里只有三处：`database_findings:472-480`（head 读不到）、`gateway_config_findings:357-362`（车道不在 `asr/tts`）、`main():528-536`（引擎与 API DSN）。其余全部走 `FAIL` 并 `return 1`。

- **真硬的部分**：桶存在性、版本化、gateway 配置一致性、SELECT 探针、20 个写列、角色守卫、head 落后/多行/关系缺失——都实测退出码 1 且没有 `exec`。默认 `restart: unless-stopped` 会把它们变成重启循环，`RestartCount` 增长，运维在日志里能看到点名行。
- **真开口**：head 的 `pending`。作者的理由是「旧部署不能被升级误判为故障」，并且 `migration_findings` 的文档串（`:253-259`）写「the column probes (they cover what this build uses)」。**这句话不成立**。探针是 8 条固定 `SELECT`（`job_dispatch_runtime.py:333-343`），覆盖的是 worker 启动路径读的列；迁移可能新增的是视图、表、索引、约束、以及**不在探针里的列**。写列检查补了一点，但它查的是列权限而不是列存在性——列不存在时它直接崩（实测 L），而崩溃发生在迁移检查**之前**（`:461` 先于 `:465`），所以「落后迁移」在这一路径上既得不到 `pending` 的体面，也得不到点名的 `FAIL`。

我把这条开口做成了实测，而不是理论推演：把库从 `0053` 退回 `0052`（删掉 `0053` 的视图、改 `alembic_version`），并撤销派发角色对 `alembic_version` 的只读 `SELECT`（模拟本片之前建立的任何角色）：

```text
pending: migration head not verifiable: the dispatch role cannot read speech.alembic_version; ...
job dispatch startup gate: OK
WORKER-EXECED
EXIT=0
```

同一状态补回 `GRANT SELECT` 后立刻变成 `FAIL: migration is behind: ... 0052 ... artifact head is 0053`，退出码 1。也就是说：**闸门能不能发现「库落后一个迁移」，取决于运维有没有补一条本片新增的授权，而补不上时的默认结果是「照常启动 + 一行 pending」**。`compose-minimal.md:718-719` 把它写成「不补也不会被误判为故障……由启动探针继续校验本版本用到的列」——前半句对，后半句高估了探针的覆盖面。见 B1。

其余 `pending` 的合理性：#1（API DSN 空）确实在创建侧 503，早于 claim（`backend/src/speechnexus_backend/app.py:458-466`：writer DSN 为空时 `job_runtime is None`；创建路由在未装配时返回 `503 job_unavailable`，见 `backend/src/speechnexus_backend/speech/interfaces/http/jobs.py:507` 与 `job_errors.py:113`），`pending` 成立；#4（引擎）创建侧 429，也早于 claim，成立；#6 不是闸门职责。**#20（车道）不成立**：`KINDS=asr,tts,voice_design` 在本参考部署里没有任何 gateway 配置，gate 说 `pending ... no engine can serve it` 然后启动 worker，worker 会真的认领 `voice_design`（它只校验 kind 在 dispatchable union 内，`job_dispatch_runtime.py:159-165`），然后在提交/发布阶段失败——正是本片要消灭的形态。见 S2。

### 3. 幂等与重复：闸门是纯读，结论可重复；桶的「存在 vs 未版本化」没有误判

- **容器重启/worker 反复重启**：闸门每次容器启动都作为 entrypoint 重跑，只做 `SELECT`/权限查询、`bucket_exists`、`get_bucket_versioning` 与读两份 JSON，不写任何状态。同一拓扑连续两次运行输出逐字节相同（`diff` 无差异），退出码均 0。
- **`network_mode: service:gateway` 重建**：`docker compose --profile job-dispatch config` 渲染出 `depends_on.gateway = {condition: service_healthy, restart: true, required: true}`；Compose 侧重建 gateway 会连带重建 worker，worker 重建即重跑闸门，结论仍由当时的前置决定（不缓存）。daemon 重启/外部重建不保证连带，runbook §9 已写明要手动 `--force-recreate job-dispatch-worker`，并在重建后重跑核对命令——这条属于部署侧，闸门无法自证。
- **「桶已存在」误判为「未版本化」**：没有。`bucket_findings`（`:290-324`）把 `bucket_exists` 与 `get_bucket_versioning` 分成两次判定：桶不存在 → 第一条 FAIL；桶存在且 `versioning.status != ENABLED` → 第二条 FAIL；两条消息不同。实测：删掉 staging 桶 → `does not exist`；重建但不启用版本化 → `is not versioned`；版本化已启用 → 通过。MinIO 对「从未配置版本化」的桶返回 `status=None`，闸门按 `!= ENABLED` 正确判红（不是把 `None` 当成「已存在即通过」）。
- **init 脚本幂等**：对已存在且未版本化的桶重跑 `init-object-storage.py` 会 `set_bucket_versioning` 并复核，输出 `bucket ready: speechnexus-staging (versioning enabled)`；桶集合用 `set` 去重，Artifact 与 result 同名时只处理一次（`deploy/init-object-storage.py:25-40`）。
- **provision 脚本不幂等（设计如此）**：第二次运行报 `roles already exist: sn_jw2, sn_ja2, sn_jd2; inspect ownership before proceeding`，退出码 1，不做任何猜测性修复。这不是漂移，但意味着**部分失败后没有重入路径**（`compose-minimal.md:343-345` 的基线段已声明失败不回滚）；本片的异步段没有重复这句，见 [S8](#s8-异步段的-provision-没有沿用失败不回滚的说明)。

### 4. 镜像构建：真的 build 成功了，闸门真能跑；COPY 守卫偏弱

- **构建**：从 worktree 以仓库根为 context、`deploy/images/python.Dockerfile` 构建，`BUILD_EXIT=0`；日志里 `COPY asset-inspector/README.md asset-inspector/pyproject.toml asset-inspector/` 与 `COPY asset-inspector/src asset-inspector/src` 两步都在，`uv sync --frozen --all-packages --all-extras --no-dev --no-editable` 输出 `Built speechnexus-asset-inspector @ file:///app/asset-inspector`。`.dockerignore` 不排除 `asset-inspector`；根 `pyproject.toml` 的 `[tool.uv.workspace] members` 含 `asset-inspector`；`asset-inspector/README.md` 与 `pyproject.toml` 存在（hatchling 的 readme 引用可满足）。镜像内 `import speechnexus_asset_inspector` 成功。
- **闸门在镜像里能跑**：以镜像默认 UID 10001 导入 `deploy/job-startup-gate.py` 成功（`minio`、`psycopg`、`alembic` 都在 venv 内；`alembic` 来自 backend 的 `migrations` extra，Dockerfile 用了 `--all-extras`），`expected_migration_head()` 解析出包内唯一 head，`DISPATCH_WRITE_SURFACE` = 20 列，探针 8+2 条。`python` 由 `PATH=/app/.venv/bin` 解析为 venv 解释器，entrypoint 形态 `["python", "/opt/speechnexus/job-startup-gate.py"]` 生效。
- **闸门不在镜像里**：`/opt/speechnexus` 在镜像中不存在，由 compose 从 host 挂载 `./job-startup-gate.py`。这是本参考部署的既有形态（compose.yaml、私有配置目录同样来自 host），但带来一个版本偏差面：闸门逻辑来自运维检出的仓库，`expected_migration_head()` 来自镜像里的包。见 [S9](#s9-闸门脚本由-host-挂载存在版本偏差面)。
- **守卫偏弱**：`tests/ci` 的 `test_the_python_image_copies_every_uv_workspace_member` 只要求每个成员出现**一条** `COPY {member}/`。我只删掉 `COPY asset-inspector/src asset-inspector/src`（保留 README/pyproject 的 COPY 行）后：`tests/ci` **14 passed**，而镜像**仍然构建成功**（hatchling 对无 `src` 的成员打出空 wheel），运行时 `import speechnexus_asset_inspector` → `ModuleNotFoundError`。即：守卫守着「成员名字出现过」，没守「源码进镜像」。见 [S7](#s7-镜像成员-copy-守卫只检查一行)。

### 5. 权限面：220 条在 `8404efad` 成立；dispatch 的 20 列是精确子集；有「多授」但已被文档免责；口令不打印

- **条数**：`8404efad` 树上 `--print-statements` 输出 **220** 条（与提交信息、`compose-minimal.md:700`、`runbook:264/672/723` 一致），`voice_validation` 车道 **222**。同一脚本在 `origin/main` 上输出 **221**，差异来自后续提交给 `voice_draft_schema.py` 加的 1 条授权——所以本片的 `220` 在合并时是对的，`origin/main` 现在已漂到 221（见 [S5](#s5-文档把精确语句数写成期望输出已经漂移)）。
- **少授（对被守护的 20 列）**：无。dispatch 角色的授权集合包含闸门要求的全部 20 列：`speech_jobs` UPDATE 5 列、`coordination_jobs` UPDATE 4 列、`speech_job_events` INSERT 8 列、`speech_job_cancellation_not_submitted` INSERT 3 列，逐条能在 `--print-statements` 输出里找到对应 `GRANT ... (列...)`。闸门检查用 `has_column_privilege` 逐列判定，不是「有表级权限就过」。
- **少授（对未被守护的写面）**：有，`speech_artifacts` 的 `INSERT`（结果发布必写）不在写面里，实测撤销后闸门仍 `OK`（B2）。
- **多授**：dispatch 角色额外持有 `coordination_runtime_grants` 带来的 `engine_instances`/`runtime_instance_heads`/`runtime_instance_intents`/`admission_*`/`resource_pools`/`host_nodes`/`workspace_quotas` 等表级 INSERT 与列级 UPDATE。在 dispatcher 的代码路径（`job_dispatch_runtime.py`、`job_dispatch_worker.py`、`job_dispatch_postgres.py`、`artifacts_postgres.py`、`job_dispatcher.py`）里这些表名命中 **0 个文件**；`coordination_runtime_grants` 是仓库既有的协调写面声明（`production-jobs.md:294` 说释放路径需要它），不是本片新造的。`deploy/README.md:283-286` 已明确「it does not audit extra DML grants, so a broader role is not rejected」——因此记为提示而非缺陷，见 [S12](#s12-dispatch-的多授面在-dispatch-代码里没有可观测用途)。
- **幂等/漂移**：不幂等（角色已存在即停，见 §3），不会覆盖既有角色的口令或授权；授权语句从 `*_grants()` 函数生成，不是手写 SQL，重复安装同一组是幂等的（`GRANT` 本身幂等），但脚本不允许走到那一步。
- **口令**：`main()` 唯一成功输出是 `job roles ready: <三个角色名>; grants: N statements`（`:228`），`ALTER ROLE ... PASSWORD` 用 `sql.Literal` 绑定、不经过 stdout。我用了三个可检索的口令（`SUPERSECRETpw_one/two/three`）在全新库上跑完整路径，捕获 stdout+stderr 后 `grep -c SUPERSECRETpw` = 0。空 DSN 的报错只点名变量名（`:89-96`）。未见泄露。

### 6. 测试强度：作者的三条反例我只复现成功两条半；没守护的是 `main()`

作者的承诺（`tests/ci` 模块头 `:11-16`）是「三处变异应使其变红」：删 `init-object-storage.py` 的 result 桶条目、删闸门的结果桶探针/解开 entrypoint 装配、丢一个 grant 组。我的独立变异（每次改完先确认文件内容，再跑两套测试）：

| # | 变异 | 预期 | 实测 |
| --- | --- | --- | --- |
| M1 | 删 `init-object-storage.py` 的 `SPEECHNEXUS_EXECUTION_RESULT_BUCKET` 回退条目 | 红 | **红**（`test_the_execution_result_bucket_is_created_and_versioned` failed，1 failed / 36 passed） |
| M2 | `referenced_buckets` 不再收集 `RESULT_BUCKET_ENV` | 红 | **红**（3 failed：两条单测 + CI 探针断言） |
| M3 | provision 脚本删掉 **dispatch 的** `cancellation_reconciliation_grants(...)` 调用（组名在 writer 处仍出现） | 红（「少一组 grant」） | **37 passed** ✘ |
| M3b | 彻底删掉 `public_job_grants` 的两个调用（组名在脚本中彻底消失） | 红 | **红**（1 failed） |
| M4 | `database_findings` 不再调用 `migration_findings` | 红 | **37 passed** ✘ |
| M5 | FAIL 分支 `return 1` → `return 0` | 红 | **37 passed** ✘ |
| M6 | `database_findings` 不再调用 `write_privilege_findings` | 红 | **37 passed** ✘ |
| M7 | `main()` 不再收集桶（`referenced_buckets(env)` 换成 `[], []`） | 红 | **37 passed** ✘ |
| M8 | 只删 `COPY asset-inspector/src`（保留成员元数据 COPY） | 红 | **14 passed** ✘ |

结论：**「少一组 grant」这条反例比声明窄**——CI 断言是 `f"{name}(" in source`，只要组名还在别的调用点（writer）出现就通过，而闸门守护的恰恰是 dispatch 那一份；M3 就是这么绿的。**真正没有守护的是 `main()`**：它一次都没被测试调用过，`tests/unit/test_job_startup_gate.py` 全部直接调用各检查函数（`:96-350`），`tests/ci` 只读文本。于是四条最要命的性质——「每个检查真的被 `main()` 调用」「失败时退出码为 1」「失败时不 `exec`」「成功时才 `exec`」——可以被静默破坏而两套测试全绿（M4–M7）。这也是我把 [B3](#b3-main-的装配退出码与反例声明没有守护) 列为阻塞的原因：当前提交里的装配是**对的**（我用真实 PG/MinIO 验证过），但没有任何机制防止下一次改动把它拆掉。

**没有用例守护的前置**：迁移头（`migration_findings` 有用例，但「它在 `database_findings` 里被调用」没有）、写列（同上）、桶（同上）、以及整个 `main()` 的退出/`exec` 契约。最危险的是「退出码」：这是闸门存在的全部意义，而 `return 1` → `return 0` 无一用例变红。

### 7. 断言强度：用例名与检查内容大体相符，三处名不副实

相符的部分：桶用例真的断言消息文本与 env 键（`:129-150`）；迁移用例同时断言两个 head 值（`:309-317`）；写列用例断言了具体列名与 grant 提示，并断言 SQL 用了 `has_column_privilege`（`:249-256`）；探针用例断言「第一条失败即停」，连语句数都验了（`:259-268`）。

名不副实的三处：

1. `test_the_python_image_copies_every_uv_workspace_member`（`tests/ci:76-90`）检查的是「成员名出现过一行 COPY」，不是「成员可构建/可导入」。M8 证明删掉 `src` 仍绿、镜像仍构建成功但包为空。
2. `test_the_provisioning_script_installs_every_grant_group`（`:180-188`）检查的是「组名出现在脚本任意位置」，不是「每个角色都装了该组」。M3 单角色少一组仍绿。
3. `test_the_gate_probes_both_job_buckets_before_the_worker_runs`（`:136-151`）里的 `assertIn("os.execvp(command[0], command)", gate)` 只验了字符串存在，没验「它在 FAIL 之后」。把 `execvp` 挪到 `if failures:` 之前，这条用例照样绿（M5 同源的强度问题）。
4. `test_a_dispatch_role_without_the_version_grant_reports_pending_not_failure`（`tests/unit:298-306`）把一个**有安全后果**的取舍固化成了期望行为：它只断言 `findings == []` 与 `head is None`，不区分「库是当前 head」与「库落后」——而这正是 B1 的开口。用例名说「reports pending not failure」，实际检查的是「**任何**读不到 head 的情况都不失败」。

另有一处表面重复：`test_the_write_surface_matches_the_read_only_self_check`（`:195-196`）只把闸门的写入面与 `check-async-job-prereqs.py` 的副本比对，**不与 worker 真正执行的 SQL 比对**。两条 `GRANT`/`UPDATE` 面将来一起漂移时，这条用例仍然是绿的。见 [S10](#s10-写入面只与另一份副本比对不与-worker-sql-比对)。

### 8. 文档一致性：步骤能从零照抄，但 `compose.env.example` 与脚本互相矛盾，且 pending 的兜底被写强了

照抄可行性（静态核对，未跑完整 `up`）：

- 顺序成立：§4 起 PG/MinIO 并 `run --rm minio-init`（桶与版本化）→ §5 迁移（`speech.alembic_version` 就位）→ §6 基线角色（含 `sn_gateway`，`compose-minimal.md:391`）→ §6 异步子段建三个 Job 角色（`provision-job-roles.py` 要求 `sn_gateway` 已存在，`:190-198`）→ §7 起 API/gateway → runbook 再起 `job-dispatch` profile。`provision-job-roles.py` 的 `GRANT SELECT ON speech.alembic_version` 依赖迁移已跑，位置正确。
- 渲染正确：`docker compose --env-file deploy/compose.env.example --profile job-dispatch config` 得到 `entrypoint=["python","/opt/speechnexus/job-startup-gate.py"]`、`network_mode=service:gateway`、两个只读挂载、`SPEECHNEXUS_JOB_GATE_PRIVATE_DIR=/run/speechnexus-private`、`depends_on` 三项齐全、`healthcheck` 不存在、`read_only=true`、`user=10001:10001`；示例私有配置 `deploy/private/gateway-file-{asr,tts}.example.json` 的 `result_bucket` 与示例 env 的 `SPEECHNEXUS_EXECUTION_RESULT_BUCKET` 都是 `speechnexus-results`，从零复制示例不会触发 G。
- **矛盾一**：`deploy/compose.env.example:248-253` 写「Provision it with `scripts/dev-env/provision-job-roles.py`, which writes the value below … The same script writes the API writer and admission DSNs」。脚本**不写任何 DSN**（全文无文件写入、无 `ROLE_PASSWORD` 引用，`grep` 为 0），它的空值报错恰恰是「write the three Job DSNs into `deploy/compose.env` first」（`:89-92`）。照这本 env 示例操作的人会先跑脚本、再被要求「先去写 DSN」，顺序与文字相反。`compose-minimal.md:645-676` 与 runbook §4 的写法是对的（先写 DSN 再跑脚本）。见 [S3](#s3-composeenvexample-声称脚本会写-dsn与脚本行为相反)。
- **矛盾二**：`compose-minimal.md:718-719` 把「head 记为 pending」解释成「由启动探针继续校验本版本用到的列」，实测不成立（探针只覆盖 8 条 SELECT；写列缺失会崩栈，视图/表/索引新增完全不可见）。见 B1 与 S1。
- **过时断言（非本片引入）**：`runbook:191/669` 仍写「在仓库当前 head 上实测为 `0048_voice_evidence_scan_index`」，`origin/main` 的实际 head 已是 `0053`（`8404efad` 当时是 `0052`）。这两行在 `8404efad^` 就存在，不是本片写的，但本片重写了该文件大部分内容、并把 head 校验做成闸门核心，仍留着自相矛盾的"实测"值。`compose-minimal.md` 的 `0052` 已被后续提交改成 `0053`（`git diff 8404efad origin/main -- docs/operations/compose-minimal.md`），说明这类写死值迟早漂移。见 [S6](#s6-runbook-的-head-实测值已过时)。
- **精确语句数风险**：`220 statements` 被写进 4 处期望输出（`compose-minimal.md:700`、`runbook:264/672/723`）。在 `8404efad` 准确，在 `origin/main` 已是 221。把计数写成验收断言会让下一个人误判「多了一条授权 = 授权面漂移」。见 S5。
- **过程性叙事**：runbook §13 保留了两段带日期的验证记录（2026-11-08、2026-11-13 增补），这属于该文件职责（操作手册的取证记录），不违反终态语体约定；但增补段标题写「#649」而本片合并是 PR #642 / issue #572，仓库内没有 `#649` 的其他引用，追踪线索断了。见 [S11](#s11-验证记录的-649-在仓库内无法追溯)。

---

## 缺陷清单

### 必须修复（阻塞）

#### B1. 迁移 head 在缺只读授权时对真实落后的库放行

- **位置**：`deploy/job-startup-gate.py:250-287`（`migration_findings` 的自证）、`:472-480`（`pending` 分支）、`:537-545`（`pending` 不进 `failures`，直接 `return 0` 并 `exec`）；文档承诺见 `docs/operations/compose-minimal.md:718-719`、`deploy/README.md:207-209`、`runbook:33`（「worker 的启动闸门点名缺桶/缺版本化/配置不一致并退出」）。
- **实测**：库停在 `0052_quarantine_releases`（删掉 `0053` 的视图并改 `alembic_version`），撤销 `sn_job_dispatch` 对 `speech.alembic_version` 的 `SELECT` → `pending: migration head not verifiable` + `job dispatch startup gate: OK` + `WORKER-EXECED`，**退出码 0**。补回 `GRANT SELECT` 后同一库 → `FAIL: migration is behind: ... 0052 ... 0053`，退出码 1。
- **为什么阻塞**：这条 `pending` 不是边缘情形，而是**本片之前所有已部署角色的默认状态**——只读 `SELECT` 是本片新加的第 220 条授权，升级镜像但没重跑 provision 的部署全部落在 `pending`。此时闸门对「迁移未执行」这个前置完全没有判定能力，只能靠固定探针偶然撞上；而探针不覆盖视图/表/索引/非探针列（`0053` 恰好只加一个 dispatcher 不用的视图，本轮侥幸无害，换一次迁移就是「claim 之后才炸」）。作者声明的取舍（「不把升级误判为故障」）本身合理，但**取舍的代价被文档写成「探针会继续校验」，实测不成立**。闸门文件头「verifies every deployment prerequisite」因此不成立。
- **最小改法**（三选一，都不需要改运行时契约）：
  1. 缺 head 授权改为 **FAIL**，把「旧部署升级前先补这一条 `GRANT`」写进 `compose-minimal.md` §6 的升级段与 `deploy/README.md` 的升级段（现在只写了「补一条只读授权即可」当建议，没有写成升级前置）；
  2. 保留 `pending`，但让它**可被观测地降级**：默认 FAIL，只有显式 `SPEECHNEXUS_JOB_GATE_ALLOW_UNVERIFIED_HEAD=1` 时才 `pending`，并在日志里打 `WARNING`；
  3. 若坚持默认 `pending`，至少把兜底从「探针覆盖本版本用到的列」改成实际可验证的检查（例如对包内迁移引入的关系名做 `to_regclass` 存在性核对），并修掉同一句里对探针能力的错述。

#### B2. 写入面缺 `speech_artifacts` INSERT（媒体发布必写）

- **位置**：`deploy/job-startup-gate.py:66-96`（`DISPATCH_WRITE_SURFACE` 只有 4 张表 20 列）；对比 `backend/src/speechnexus_backend/speech/application/results.py:149-175`（`_publish` 在 claim 之后 `insert_ready` 写 `speech_artifacts`）与 `:219-240`（`_publish_validation` 同样写）、`artifact_schema.py:65-78`（`artifact_publisher_grants` 声明 INSERT 列）。
- **实测**：`REVOKE INSERT ON speech.speech_artifacts FROM sn_jd2`（SELECT 仍在，探针 `SELECT artifact_id FROM speech.speech_artifacts LIMIT 0` 通过）→ 闸门 `ok: database: ... 20 post-claim write columns` + `job dispatch startup gate: OK` + `WORKER-EXECED`，退出码 0。
- **为什么阻塞**：这正是本片声称消灭的形态——角色通过闸门、领取 Job、在第一次结果发布时因缺 `INSERT` 失败。`deploy/README.md:196-212` 写的「the post-claim write columns … are granted, so a role missing `job_dispatch_grants` stops at container start」对 artifact 发布这条路径不成立。`job_dispatch_grants` 里确实包含 `artifact_publisher_grants`，所以走新建脚本的部署不会命中；命中的是**部分返修/手工补授权**的既有部署，而文档正在鼓励「按授权函数重放」。
- **最小改法**：把 `artifact_publisher_grants` 声明的 INSERT 列并入 `DISPATCH_WRITE_SURFACE`（现有单测 `test_every_write_column_the_gate_requires_is_declared_by_a_grant_function` 会自动跟着覆盖），或对 `speech_artifacts` 加一条 `has_table_privilege(current_user, 'speech.speech_artifacts', 'INSERT')` 的表级判定（发布路径只做 INSERT，不需要逐列）。同时补一条用例：撤销后必须红。

#### B3. `main()` 的装配、退出码与反例声明没有守护

- **位置**：`deploy/job-startup-gate.py:488-548`（`main()` 从未被任何测试调用）；`tests/unit/test_job_startup_gate.py:96-350`（全部直接调用检查函数）；`tests/ci/test_reference_deploy_job_prereqs.py:180-188`（grant 组只查「名字出现」）、`:136-151`（`execvp` 只查字符串存在）。
- **实测**：`return 1` → `return 0`、摘掉 `migration_findings` 调用、摘掉 `write_privilege_findings` 调用、把 `main()` 的桶收集改成空列表，四种变异各自 **37 passed**；把 dispatch 的 `cancellation_reconciliation_grants` 删掉（组名仍出现在 writer 调用里）**37 passed**。
- **为什么阻塞**：闸门的全部价值是「失败时退出 1 且不 `exec`」。当前提交的装配经我实跑是对的，但这一性质只靠人读代码；任何一次重构（把检查挪到 `exec` 之后、把一个检查从 `main()` 摘掉、把退出码改成 0 让 Compose 别重启）都不会变红。作者声明的第三条反例（「丢一组 grant 会变红」）在单角色粒度上不可复现，而单角色粒度才是闸门真正检查的东西。
- **最小改法**：
  1. 把 `main(argv, execute=os.execvp, env=os.environ, connect=..., client=...)` 改成可注入，然后补一组用例：`failures` 非空 → 返回 1 且 `execute` 未被调用；无 findings → 返回 0 且 `execute` 被调用且参数是 `command`；
  2. 补一组「每个检查被调用」的用例（`monkeypatch` 各 `*_findings` 记录调用）；
  3. `tests/ci` 的 grant 组断言改成「每个角色各自的调用点」（例如按 `statements(...)` 的渲染结果断言每个角色都拿到五组），而不是「组名在文件里出现过」；
  4. `execvp` 断言改成断言它在 `if failures:` 之后（例如用 AST 或对 `main` 的行为测试，而不是 `assertIn` 文本）。

### 建议改进（不阻塞）

| # | 项 | 依据 | 建议 |
| --- | --- | --- | --- |
| S1 | 缺写列时崩栈而不是点名 FAIL | 实测 L：库缺 `speech_jobs.result_json` → `psycopg.errors.UndefinedColumn` traceback，退出码 1、未 exec，但没有任何 `FAIL:` 行 | `write_privilege_findings` 照 `probe_findings` 的样式 `except Exception` 包一层，输出 `FAIL: <schema> 缺少后置写入列（迁移未执行或落后）`；同时把迁移检查提到写列检查之前，让「落后」优先被点名 |
| S2 | 本参考部署无法服务的车道记为 `pending` 并启动 | 实测 J/K：`KINDS=asr,tts,foo` 与 `KINDS=asr,tts,voice_design` 都 `pending` + `OK` + exec；worker 只校验 kind 在 dispatchable union 内（`job_dispatch_runtime.py:159-165`），会真的认领 | `GATEWAY_CONFIGS` 之外的 kind 分两类：不在 `DISPATCHABLE_JOB_KIND_UNION` 内 → FAIL（拼写错误）；在 union 内但本 bootstrap 无配置 → 由 compose/文档明确「该车道不在本参考部署内」，并改为 FAIL（可选车道必须整体启用或整体不选） |
| S3 | `compose.env.example` 声称脚本会写 DSN，与脚本行为相反 | `deploy/compose.env.example:248-253` vs `provision-job-roles.py:85-96`（空值即退出）与 `grep ROLE_PASSWORD` = 0 | 改写成「脚本从下面三条 DSN 读取角色名与口令；三条 DSN 需先写入（见 runbook §4）」，并删掉「The same script writes ...」 |
| S4 | 六项前置的覆盖边界没有写在闸门契约里 | `job-startup-gate.py:1-9`、`deploy/README.md:196-212` 用「every prerequisite」表述，实际硬失败 3 项、pending 2 项、不检查 1 项 | 在文件头与 README 列出「硬失败 / pending / 不在闸门职责」三栏，逐项对应 runbook 的六项编号 |
| S5 | 文档把精确语句数写成期望输出，已漂移 | `8404efad`=220，`origin/main`=221（`voice_draft_schema.py` 后续 +1）；4 处写死 `220 statements` | 期望输出改成「`grants: <N> statements`，首次安装后记录 N 作为基线」，或在文档里附「本基线为 220（`8404efad`）」并说明它随授权函数变化 |
| S6 | runbook 的 head「实测值」已过时 | `runbook:191/669` 写 `0048_voice_evidence_scan_index`；`origin/main` 实际 `0053`，`8404efad` 为 `0052` | 删掉具体值，只保留「与发布制品的唯一 head 一致」；或改为引用命令输出而不写值 |
| S7 | 镜像成员 COPY 守卫只检查一行 | 实测 M8：删 `COPY asset-inspector/src` → 14 passed，镜像仍构建成功但 `import speechnexus_asset_inspector` 失败 | 断言每个成员同时有 `<member>/pyproject.toml`、`<member>/src`（或该成员声明的实际产物）两行 COPY；更稳的做法是让 CI 真的构建镜像并按成员逐个 `import` |
| S8 | 异步段的 provision 没有沿用「失败不回滚」的说明 | `compose-minimal.md:343-345` 只在基线段写了「失败不会整体回滚……直接重跑会因已有角色而停止」；异步子段 `:645-700` 只说「角色已存在时直接停止」 | 在异步子段补一句失败语义与恢复步骤（先核对 `pg_roles`/ACL，按记录回退后再重跑），避免运维在部分失败后手工猜 |
| S9 | 闸门脚本由 host 挂载，存在版本偏差面 | `compose.yaml:438-440`（`./job-startup-gate.py:/opt/speechnexus/job-startup-gate.py:ro`）；镜像内无 `/opt/speechnexus` | 在 `deploy/README.md` 说明「闸门逻辑随检出，`expected_migration_head()` 随镜像；升级镜像时同步检出」，或在镜像里 COPY 一份并用 host 挂载覆盖（保持行为不变） |
| S10 | 写入面只与另一份副本比对，不与 worker SQL 比对 | `tests/unit:195-196` 把闸门面与 `check-async-job-prereqs.py` 的副本做等式；没有用例把 20 列与 `job_dispatch_postgres.py`/`results.py` 的真实语句绑定 | 加一条静态用例：从 worker 的 `UPDATE/INSERT` 语句里抽取列名（或维护一张「写面来源」表并断言两侧相等），闸门面漂移时能红 |
| S11 | 验证记录的 `#649` 在仓库内无法追溯 | `runbook:709`「2026-11-13 增补（#649，基线 `origin/main` `60b70d57`）」；本片合并是 PR #642 / issue #572，`grep -rn "#649"` 仅此一处 | 改成实际 issue/PR 编号，或注明「#649 为本轮工作卡」并给出链接 |
| S12 | dispatch 的多授面在 dispatch 代码里没有可观测用途 | `--print-statements` 输出里 dispatch 持有 `engine_instances`/`runtime_instance_*`/`admission_*`/`resource_pools`/`host_nodes`/`workspace_quotas` 的写权限；`grep` 这些表名在 dispatcher 五个模块里命中 0 | 记为提示：`deploy/README.md:284-287` 已声明「不审计多余 DML」，但建议在 `production-jobs.md` 补一句「dispatch 的 `coordination_runtime_grants` 是释放/协调路径所需，dispatcher 本进程不直接写这些表」，减少下一位维护者的误判 |

---

## 我独立复现的反例（原始记录）

### 测试级变异

所有变异都先打印变异后的关键片段确认生效，跑完 `git checkout --` 还原；还原后 `git status --porcelain` 为空。

```text
######## M1: delete the result-bucket entry from init-object-storage.py ########
FAILED tests/ci/test_reference_deploy_job_prereqs.py::ObjectStorageInitTests::
       test_the_execution_result_bucket_is_created_and_versioned
1 failed, 36 passed in 0.35s

######## M2: gate stops collecting the result bucket ########
FAILED tests/unit/test_job_startup_gate.py::test_the_result_bucket_is_probed_even_when_it_matches_the_artifact_bucket
FAILED tests/unit/test_job_startup_gate.py::test_a_blank_bucket_variable_names_the_key
3 failed, 34 passed in 0.35s

######## M3: provision script drops cancellation_reconciliation_grants for dispatch ########
37 passed in 0.33s          # ← 声明应红，实际全绿

######## M3b: remove both public_job_grants calls (name disappears) ########
FAILED tests/ci/test_reference_deploy_job_prereqs.py::JobRoleProvisioningTests::
       test_the_provisioning_script_installs_every_grant_group
1 failed, 36 passed in 0.34s

######## M4: database_findings no longer calls migration_findings ########
37 passed in 1.63s          # ← 迁移检查整段摘掉仍全绿

######## M5: FAIL path returns 0 instead of 1 ########
mutated; 'return 1' count now: 0
37 passed in 0.34s          # ← 退出码改成 0 仍全绿

######## M6: database_findings no longer calls write_privilege_findings ########
37 passed in 0.32s

######## M7: main() no longer collects buckets ########
37 passed in 0.32s

######## M8: only COPY asset-inspector/src removed ########
14 passed in 0.01s          # ← 镜像仍构建成功，但包为空（import 失败）
```

镜像侧证据（M8 的后果）：

```text
#25 0.372  Built speechnexus-asset-inspector @ file:///app/asset-inspector
#29 ... naming to docker.io/library/sn-review-python:655-nosrc done
BUILD2_EXIT=0
$ docker run --rm --entrypoint python sn-review-python:655-nosrc -c "import speechnexus_asset_inspector"
ModuleNotFoundError: No module named 'speechnexus_asset_inspector'
```

### 运行时变异

隔离拓扑：`postgres:16.15-alpine` + 仓库 `speechnexus-minio:RELEASE.2025-09-07T16-13-09Z`，迁移与桶初始化用镜像内的真实入口，角色用真实 `provision-job-roles.py` 创建。`SPEECHNEXUS_OBJECT_STORAGE_BUCKET=speechnexus-results`、`SPEECHNEXUS_EXECUTION_RESULT_BUCKET=speechnexus-staging`（**故意不同名**，以同时验证两条桶判定与配置一致性判定）。

**A. 六项前置齐备（期望 OK + exec）**

```text
job dispatch startup gate: deployment prerequisites before any claim
  ok: database: role guard, startup SELECT probes and 20 post-claim write columns, migration head 0053_voice_draft_publication
  ok: object storage: versioned buckets speechnexus-results, speechnexus-staging
  ok: gateway staging bucket matches gateway-file-asr.json, gateway-file-tts.json
  pending: real engine and a registered ready instance are a deployment input this gate cannot verify; ...
  pending: API-side writer/admission DSNs live in the api container; ...
job dispatch startup gate: OK
WORKER-EXECED as 10001
EXIT=0
```

连跑两次，输出 `diff` 无差异。

**B. 撤销派发角色对 `speech_job_cancellation_not_submitted` 的 `INSERT`**

```text
  FAIL: dispatch role lacks speech_job_cancellation_not_submitted.job_id (INSERT); ...
  FAIL: dispatch role lacks speech_job_cancellation_not_submitted.observed_at_us (INSERT); ...
  FAIL: dispatch role lacks speech_job_cancellation_not_submitted.observer (INSERT); ...
job dispatch startup gate: 3 prerequisite(s) missing; refusing to start speechnexus-job-dispatch-worker
EXIT=1    # 无 WORKER-EXECED
```

**C. 撤销 `SELECT ON speech.speech_artifacts`（探针面）**

```text
  FAIL: dispatch role cannot run the startup probe on speech_artifacts:
        install job_runtime_grants, job_dispatch_grants, public_job_grants,
        cancellation_reconciliation_grants, coordination_runtime_grants for this role
EXIT=1
```

**D1. 库退回 `0052`，`alembic_version` 可读**

```text
  FAIL: migration is behind: speech.alembic_version is 0052_quarantine_releases,
        the artifact head is 0053_voice_draft_publication; run the migration profile before the job-dispatch profile
EXIT=1
```

**D2. 同一落后库 + 撤销 `SELECT ON speech.alembic_version`（= 本片之前的角色）**

```text
  pending: migration head not verifiable: the dispatch role cannot read speech.alembic_version;
           grant the read-only SELECT from docs/operations/compose-minimal.md §6 ...
job dispatch startup gate: OK
WORKER-EXECED
EXIT=0
```

**E/F. 结果桶缺失 / 存在但未版本化**

```text
# E（删掉 speechnexus-staging，Artifact 桶仍在）
FAIL: bucket speechnexus-staging (SPEECHNEXUS_EXECUTION_RESULT_BUCKET) does not exist: minio-init creates and versions it; ...
EXIT=1

# F（重建但不启用版本化：get_bucket_versioning().status 为 None）
FAIL: bucket speechnexus-staging (SPEECHNEXUS_EXECUTION_RESULT_BUCKET) is not versioned:
      the Job result read-back requires object versions; minio-init enables them
EXIT=1

# 再跑一次真实 init 脚本（桶已存在、未版本化）
bucket ready: speechnexus-staging (versioning enabled)   # 修复而不是误判
```

**G. gateway 私有配置与 env 不一致**

```text
FAIL: gateway-file-asr.json objects.result_bucket=speechnexus-results but
      SPEECHNEXUS_EXECUTION_RESULT_BUCKET=speechnexus-staging:
      the worker would fail after claiming the first Job; make them identical
EXIT=1
```

**H. 派发角色持 schema `CREATE`**

```text
FAIL: dispatch role sn_job_dispatch is a superuser, owns schema CREATE, owns the database
      or is a member of a table owner in speech; provision the dedicated non-owner role ...
EXIT=1
```

**I. `KINDS` 含 `voice_validation` 但未装该组授权**

```text
FAIL: dispatch role cannot run the startup probe on speech_voice_draft_revisions: ...
      voice_validation also needs voice_validation_worker_grants
EXIT=1
```

**J/K. 未知车道与未装配车道**

```text
# J: SPEECHNEXUS_JOB_DISPATCH_KINDS=asr,tts,foo
pending: gateway lane foo is not assembled by this bootstrap; its result_bucket cannot be verified and no engine can serve it
job dispatch startup gate: OK
WORKER-EXECED
EXIT=0

# K: SPEECHNEXUS_JOB_DISPATCH_KINDS=asr,tts,voice_design
pending: gateway lane voice_design is not assembled by this bootstrap; ...
EXIT=0
```

**L. 库缺一个后置写入列（`result_json` 改名模拟落后迁移）**

```text
  File "/opt/speechnexus/job-startup-gate.py", line 461, in database_findings
    write_failures, checked_writes = write_privilege_findings(connection, schema)
  File "/opt/speechnexus/job-startup-gate.py", line 227, in write_privilege_findings
    rows = connection.execute(write_privilege_sql(schema)).fetchall()
psycopg.errors.UndefinedColumn: column "result_json" of relation "speech_jobs" does not exist
EXIT=1    # 未 exec，但没有 FAIL: 行
```

对照：`psql` 直接验证 `has_column_privilege('sn_job_dispatch','speech.speech_jobs','no_such_column','UPDATE')` 报 `ERROR: column "no_such_column" of relation "speech_jobs" does not exist` —— 即这条查询对不存在的列**报错而不是返回 NULL/true**，所以是「未捕获异常」，不是「静默通过」。

**M. 撤销 `INSERT ON speech.speech_artifacts`（发布必写）**

```text
f   # has_column_privilege(...,'artifact_id','INSERT') = false
  ok: database: role guard, startup SELECT probes and 20 post-claim write columns, migration head 0053_voice_draft_publication
job dispatch startup gate: OK
WORKER-EXECED
EXIT=0
```

### 其它实测事实

```text
# 镜像
BUILD_EXIT=0
$ docker run --rm --entrypoint python sn-review-python:655 -c "..."
id: 3.14.9 uid import OK; ENABLED= Enabled
expected head = 0053_voice_draft_publication
write surface columns = 20
probes = 8 + 2
$ docker run --rm --entrypoint python sn-review-python:655 -c "import speechnexus_asset_inspector"
/app/.venv/lib/python3.14/site-packages/speechnexus_asset_inspector/__init__.py

# 授权条数（同一条命令，两棵树）
origin/main:    job roles ready: ...; grants: 221 statements   （voice_validation 车道 223）
8404efad 树:    --print-statements 计 220 条                    （voice_validation 车道 222）

# 口令
job roles ready: sn_jw2 sn_ja2 sn_jd2; grants: 221 statements
grep -c SUPERSECRETpw  <捕获的 stdout+stderr>  → 0

# 再次运行（非幂等，退出码 1）
roles already exist: sn_jw2, sn_ja2, sn_jd2; inspect ownership before proceeding

# compose 渲染
services: ['api','gateway','job-dispatch-worker','minio','minio-init','postgres']
entrypoint: ['python', '/opt/speechnexus/job-startup-gate.py']
network_mode: service:gateway
worker DB url env: ''            # 示例 env 下为空 → 闸门 fail-closed
depends_on: {'gateway': {'condition':'service_healthy','restart':True,'required':True},
             'minio-init': {'condition':'service_completed_successfully','required':True},
             'postgres': {'condition':'service_healthy','required':True}}

# 回归与静态检查
python -m unittest discover -s tests/ci        → Ran 108 tests, OK（8404efad 树 91，与作者记录一致）
pytest tests/ci/... tests/unit/test_job_startup_gate.py -q → 37 passed
ruff check / ruff format --check（4 个新增/改动 Python 文件）→ All checks passed / already formatted
```

---

## 未验证项（只能依赖作者陈述或 CI）

下面这些我**没有**独立取证，只能读作者留下的记录或代码路径；报告里任何超出这些范围的断言都不成立：

1. **真实引擎与 `ready` 实例的端到端链路**（#572 的最终验收）：claim → 提交 → gateway 执行 → 结果读回 → 发布。作者与我都明确没有跑（`runbook:734-741` 列为未验证）。闸门对第 4 项只打印 `pending`，我无法证明「有 ready 实例但引擎坏」时会不会在 claim 之后才炸。
2. **完整参考部署 `docker compose up -d --build`（api/gateway/minio/postgres 全套）**，以及 `network_mode: service:gateway` 下真实的命名空间重建。我做到的是：镜像真构建、compose config 真渲染、闸门在**等价隔离拓扑**（同一 PG/MinIO 镜像、同一 init/provision/gate 脚本）上真跑。完整栈的 API/gateway 构建与启动没有做。
3. **API 侧行为**：`Public Job writer startup rejected` 的具体日志文案、writer 角色的 8 张表 `SELECT` 与 `coordination_jobs` 4 列 `UPDATE` 在真实 API 启动校验里的表现。我只读了 `job_runtime.py` 与 `app.py` 的代码路径，没有启动 API 容器。`runbook:238-240` 说的「实测 `curl(7)`／`http_code=000`」是作者记录，我没有复现。
4. **writer/admission 两个角色的完整授权面是否「完全一致」**：我逐条核对了 dispatch 的 20 列与读取探针（少授/多授两侧都看了），writer 的 219/220 条只做了条数与角色归属核对，没有逐条比对其运行时声明面。`#649` 作者的 `JobRuntime.open()` 实测记录是唯一证据。
5. **`coordination_runtime_grants` 对 dispatch 的写面是否真被使用**：代码路径 grep 为 0 命中，但释放/协调可能经其他入口使用；我只依据 `production-jobs.md:294` 的文字声明，未做运行时取证。
6. **自检脚本 `check-async-job-prereqs.py` 在 Python 3.9/3.12 下的兼容性**（作者记录为「均通过，语法按 3.9 校验」），以及它 12 组输入矩阵的实测结果。我只核对了它与闸门的写入面等式（单测）和 `--offline`/阶段语义的代码，没有跑那 12 组。
7. **`mypy --platform linux`、`lint-imports`、`check-doc-refs.py`**：作者记录全部通过；我只跑了 `ruff`、两套测试与 `unittest discover`。
8. **`minio-init` 在完整 compose 下的输出与 `service_completed_successfully` 的门控**：我用同一脚本在隔离 MinIO 上跑过（输出一致），但 compose 编排层面的依赖门控只做了配置渲染核对。
9. **`8404efad` 的 `220` 是「当时正确」这一判断**：我用 `git archive 8404efad` 抽取该提交树、以同一脚本 `--print-statements` 得到 220，属于实测；但该提交时的测试矩阵（91 项）与 head（0052）只在本机复算，未在作者的两段验证环境（`sn-572`、2026-11-13 worktree）里重放。

---

## 收尾

- 本报告只新增 `docs/reviews/2026-11-13-reference-deploy-gate.md`，**未改动任何产品代码**；所有临时变异已 `git checkout --` 还原，`uv.lock` 未变（全程未在 worktree 跑 `uv`，测试用既有 venv + `PYTHONPATH`）。
- 隔离容器 `sn655-pg`、`sn655-minio`、网络 `sn655-net`、镜像 `sn-review-python:655`/`:655-nosrc`、临时目录与日志已删除。
- 结论与阻塞项交返修卡处理：B1（迁移 head 的 `pending` 取舍）、B2（写入面补 `speech_artifacts`）、B3（`main()` 的装配/退出码/反例守护）。
