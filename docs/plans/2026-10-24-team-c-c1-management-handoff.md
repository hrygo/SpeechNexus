# 团队 C：C1列表、状态与删除 HTTP 交接

核验日期：2026-10-24。当前单元一次独立终审完成，Critical0/Important0/Minor2；完整C00–C08 Goal及G0–G5保持未完成。

## 具名基线与制品

- base_sha=head_sha `3b8052040111cdd8af3a50aaf5b0e8b3922e1f42`，分支 `codex/team-c-c00-versions`。未commit/push，未启用生产服务。
- 从e0d安全吸收B #78：215个旧C文件字节保持，两份README保留双方内容；旧217文件补丁在干净目标完整apply后，所有实际字节均与预检一致。未执行VAD/模型或把B资格算作C实时ready。
- 机器契约仍 `0.1.0.dev0`，使用3b既有A生成器；仅改C-owned workbench source并重生成现有产物，不改生成逻辑/共享锁。六个新管理操作补400/408/413/500/503，两个删除写操作补415；共享HTTP正负样例298条由Python/TypeScript消费。
- 当前累计制品以本机忽略目录 `.runtime/team-c/team-c-current.manifest.json` 的base/逐文件SHA/patch校验值为准；上一e0d/217文件补丁SHA `7331e85785e4440e5adf9f522fd53b82664b91bad17675b78116a84eceeb82f7`保留。制品未发布到main。

## 已实现行为

`backend/src/speechnexus_backend/workbench/app.py:create_app` 在既有七个项目/稿件版本操作上组合六个管理操作，完整覆盖当前C1的13项操作。调用方显式注入 `versions`、可选 `deletions`、`default_workspace_id`、`deployment_id`、`cursor_key`、`max_json_bytes` 和 `body_timeout_seconds`；不隐式创建服务、数据库、密钥或迁移。组合根注入既有infra删除投影，HTTP层不反向依赖infra。共享生产root仍由A装配。

项目/稿件列表使用 `(created_at DESC,id DESC)` live keyset，参数默认值从生成契约读取，默认50、1–100；列表仅含metadata。cursor保留精确UTC微秒，采用canonical base64url和HMAC，绑定部署/资源种类/父作用域；篡改、错scope/key、截断和非canonical值400，不能静默回第一页。key由调用方提供稳定的32–4096字节配置，不落盘或输出；重启使用相同deployment/key可继续旧cursor，key丢失后旧cursor失效，需重新读取首屏。分页期间插入新记录不让旧cursor回头，已删记录不再出现；这不是跨页一致快照或身份权限边界。

status只按实际注入服务返回workbench_enabled；该工厂未接LLM，始终llm_enabled=false、capabilities=[]。OpenAPI按实际注册操作过滤；既有七操作独立app保持七项，新工厂列13项。默认Swagger/ReDoc/CDN辅助页继续关闭，保留本地schema。405 Allow已覆盖同路径GET/PATCH，消费当前FastAPI的iter_route_contexts而非假设include_router后是平铺APIRoute。

删除沿用版本固定的预检/完整目标确认、事务内tombstone/outbox/回执和独立清理账本。三条删除操作增加可选workspace_id query，默认部署workspace；错误scope返回404，不泄漏plan/record。普通删除阻断子依赖，cascade必须完整确认；revision/digest/依赖变化或过期409，确认集不匹配422，owner错误422。当前只实现project/transcript图，其他已声明workbench种类明确422 unsupported。202保持原queued/pending快照；GET只在真实cleanup ledger后变succeeded/complete，原键重放仍返回最初202，不能冒充清理已完成。数据及内部SQL/异常不进入响应，未知提交保留原键、不自动重试。

## 实测与边界

从仓库根运行，Python3.14.9/PostgreSQL18.6/macOS arm64；现有Node26.11.1与`.nvmrc`26.12.1差异继续如实记录，未升级工具链。

| 验证 | 实际结果 |
| --- | --- |
| `uv run --locked pytest tests/unit tests/contract -q` | 2414 passed，38.45s，无测试skip |
| 游标/两类HTTP/错误/生成边界针对性 | 454 passed，0.89s |
| 游标/HTTP ingress/router/组合工厂覆盖 | 77 passed，语句与分支组合94.66% |
| 私有socket PG探针 `scripts/dev-env/workbench-pg-probe.py --pg-bin <existing-pg-bin> --coverage --output <result>` | 269 passed，14.84s，无测试skip，组合覆盖94.66% |
| Python format/ruff/mypy/import-linter | format471，mypy349，7项依赖规则通过 |
| `make contracts-check` | 28源契约及生成drift通过 |
| `pnpm format:check`/`lint`/`typecheck`/`test`/`build` | 全部通过；891=contracts526+Admin79+Workbench286 |

PG探针仅使用本项目临时socket-only集群、专用DB/owner/最小runtime/cleanup角色；真实Uvicorn监听127.0.0.1随机端口，httpx不读取环境代理。覆盖13操作、并发CAS、精确历史、同时间分页及插入/删除穿插、跨scope拒绝、ordinary/cascade/漏confirm、计划过期重放/新增依赖、project/单transcript清理、GET账本与POST原接受重放。三个本轮临时集群均已独立确认pg_ctl status退出3且无postmaster.pid；最终证据 `.runtime/team-c/c1-management-3b8052/pg-final-stopped.json`。

TDD记录：初始游标缺模块RED、契约错误32项RED后重生成GREEN；HTTP初次4 failed/43 passed，沿当前FastAPI路由上下文修复后47项通过。PG首轮1 failed/267 passed为新增测试key长度不满足既有min8，改合成key而未放宽契约；最终新增第四条单稿件删除用例后269项通过。

单元与网络测试用合成内容；无新真实浏览器/设备/模型/GPU/Linux/出站或恢复资格。既有Chromium合成证据仍按各自交接范围；只验证有界单请求，不表示整站容量/真实COMMIT ACK丢失或所有并发恢复故障已验证。

## 下一接收方与未完成项

A按具名基线集成C-owned工厂/owner迁移和新增source语义，并用同一生成流水线更新消费者；C继续工作台页面、Meeting/Minutes/realtime、独立副本/恢复图及C03/C05/C06/C07完整业务。C2 #77最近核验仍open/head9a913ae，C3按#19实施中；不等待整包、不吸收未合并制品。未发新跨chat消息。

一次独立终审：Critical0/Important0/Minor2；审查者另跑116项测试，确认16个冻结源hash一致，读取作者PG/门禁证据而未重新启动PG；无修复轮次或再次复审。按用户效果重分级后两项仍为Minor：拒绝与状态码正确、无错误翻页/泄漏/自动重试，当前消费者不按两种细分码作不同恢复；记录延后，不在本单元顺手修改。

- M1：空cursor或超过512字符先由生成字段校验返回400 invalid_request；结构合法但解码/验签失败返回400 invalid_cursor。source概括文案“malformed”范围偏宽，待限定描述或统一细分码。
- M2：预检主目标为外部owner返回422 deletion_owner_mismatch；接受plan时传入合法speech/artifact确认项，应用固定目标集校验返回422 deletion_confirmation_mismatch。source的foreign owners概括文案未区分这两种情况，待明确描述或统一owner规则。

完整业务、生产root/迁移和真实资格均不随本HTTP单元关闭；issue在开工前与交付后维护任务状态并回读确认。审查者未裁决的生产key管理、workspace产品/身份、A装配、未来能力发现、其他资源清理/停止/预算和图谱/旧累计交付全面复审均保持后续任务，不作为通过资格；逐条裁决及代价保存在本单元runtime账本。
