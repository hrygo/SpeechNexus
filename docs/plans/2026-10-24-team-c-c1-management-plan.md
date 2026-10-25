# C1 列表、状态与删除 HTTP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans inline; one fresh final review, one Important/Critical TDD fix pass, no commit/push.

**Goal:** 完成当前C1全部13项操作的可运行C-owned组合工厂，使项目/稿件可分页发现并以固定版本预检、确认删除、查询清理事实。

**Architecture:** 在既有七个version操作上组合六个management操作。HTTP仅依赖application/domain和生成契约，组合根注入现有infra删除投影；PG/底座表所有权不变。游标为部署/列表/父作用域绑定的服务端不透明HMAC keyset token，不建立身份边界。

**Tech Stack:** 既有FastAPI/httpx/PostgreSQL18、stdlib HMAC/struct/base64、A既有生成流水线。

**Spec:** `contracts/workbench-openapi.yaml`、`contracts/shared/deletion.schema.json`、`docs/architecture/speech-api.md` §1.6、`data-deletion.md`、C00 #35/C08 #43与完整Goal。

## Global Constraints

- base/head `3b8052040111cdd8af3a50aaf5b0e8b3922e1f42`。旧217文件完整保留，215原字节、两份README双方内容，integration证据在 `.runtime/team-c/c1-management-3b8052/result.json`。
- 不改A生产组合根/共享迁移/锁/生成逻辑，不运行VAD、模型、设备或GPU；C2未合并制品不吸收。
- 请求/响应使用生成验证，source先冻结错误/分页语义并重生成。公开分页降序(created_at,id)，limit默认50、1–100；HTTP默认值从生成契约读取，不能独立复制。
- 游标绑定deployment/kind/parent，稳定配置key由调用方注入（至少32字节，不落盘、不记录）；不引入登录/权限或用户隔离。opaque binary v1保留精确UTC微秒，最大token远低于512。
- 删除workspace_id query可选，默认部署workspace；三条删除操作采用同一作用域，原key保持。同名key可在不同workspace独立，错误作用域不泄漏计划/记录。不增加请求body副本。
- 删除只实现现有project/transcript owner图；其他已声明workbench种类明确422 unsupported，而非假成功。无stop/资源释放证据不归还预算；接受202不代表cleanup完成。
- 状态只按实际注入服务报告workbench_enabled；本工厂未接LLM，llm_enabled=false，capabilities=[]不虚构available。
- 默认文档CDN继续关闭；已有405 Allow Minor在本单元通过新TDD补完整methods。

## Review Focus

1. 跨部署/列表/父作用域、篡改和重启后cursor；不能靠token建立安全隔离或静默从第一页重读。
2. 相同创建时间、修改/新插入/删除穿插分页；无重复条目，metadata列表不能带正文，has_more与next_cursor一致。
3. 删除原计划/接受key在过期/清理完成后重放，保留原接受快照；GET按真实ledger事实更新，不能提前标complete。
4. scope/owner错配、漏confirm/新增依赖/CAS/过期、提交未知；稳定状态且无凭据/正文泄漏，不自动重试。
5. 动态OpenAPI仅实际13操作；服务未注入时准确不可用，所有methods Allow完整，辅助路由无公网资源。

### Task 1: 权威HTTP语义与分页cursor

**Files:** source/既有生成产物/共享HTTP fixtures；新增 `interfaces/cursors.py`、`tests/unit/backend/test_workbench_cursors.py`。
**Interfaces:** `CursorCodec(deployment_id: str, key: bytes)`、encode(ListingPosition)->str、decode(token,kind,parent_id)->ListingPosition；InvalidCursorError无内容错误。

- [x] 错误状态/新增workspace query与cursor边界测试先RED。
- [x] 补六个操作400/408/413/500/503，删除写操作415，source明确排序/作用域/错误；生成器不改，重生成验证GREEN。
- [x] 实现canonical base64url的v1/UTC微秒/item_id/HMAC token；验证tamper/wrong scope/key/truncated/oversize、不输出key。

### Task 2: 六个management操作与C组合根

**Files:** 新增 `interfaces/http_management.py`、`workbench/app.py`、`tests/unit/backend/test_workbench_http_management.py`；修改既有http_versions动态OpenAPI/Allow汇总。
**Interfaces:** `create_app(versions,deletions=None,*,default_workspace_id,deployment_id,cursor_key,max_json_bytes,body_timeout_seconds)`；`DeletionResponses`注入已有infra plan/accepted/current投影，无peer反向依赖。

- [x] 写真实ASGI状态/项目与稿件分页、服务关闭、参数拒绝、全部13操作及Allow回归并观察RED。
- [x] 实现management router，复用既有bounded ingress/固定错误映射/生成响应校验；新增删除命令映射和逐类错误。
- [x] 测试原键/多workspace/unsupported kind/异常不泄漏、OpenAPI只列注册操作；GREEN。

### Task 3: 真实网络/PG与交接

**Files:** 新增 `tests/integration/test_workbench_http_management.py`，更新PG probe、handoff/goal/index。

- [x] 真实loopback+隔离PG分页与顺序/并发CAS、ordinary阻断、cascade完整确认、事务后不可见、cleanup GET与接受原键重放，测试必要故障/错误作用域。
- [x] 最小充分Python/PG/契约/Web门禁；一次终审Critical0/Important0/Minor2，无必要修复，保留Minor与全部裁决。
- [x] 累计包干净3b8052 apply/全字节/307本地链接/index保护，更新九张issue与内部任务并回读；所有Gate/完整Goal继续未完成。
