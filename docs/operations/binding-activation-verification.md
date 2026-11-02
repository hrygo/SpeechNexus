# 激活幂等与同版 PostgreSQL 验证

本说明对应 DEL-327 及 DEL-332 审查修订，base `ab740baa703df754d9f40c145a3d4e3667ffc81c`
加交接 manifest 固定的候选；admin 草案为 `0.1.0.dev5`。
[ADR 0013](../decisions/0013-binding-lifecycle-admin-http.md) 整体仍为 proposed。
用户已于 2026-10-30 选择结果绑定与 head 变更竞争语义；本说明不回写原
[#273](https://github.com/hrygo/SpeechNexus/issues/273) AC 正文或历史勾选。

## 请求与恢复样例

初始 head=2，revision 1、2 都已发布且未退役，K 是合法 `Idempotency-Key`。

| 顺序 | 请求/响应 | 权威状态要求 |
| --- | --- | --- |
| 1 | K，target=1，expected=3 → `409 configuration_changed` | head、catalogue、event 均不变，K 未绑定 |
| 2 | 重新确认后 K，target=1，expected=2 → 成功 | head=1，catalogue +1，一条事件绑定本次意图 |
| 3 | K，target=1，expected=3 → `409 idempotency_conflict` | 已提交 key 不可改意图 |
| 4 | 另一命令把 head 改回 2，再用 K 完整重放步骤 2 → 原结果 revision 1 | head 仍为 2，catalogue/event 不变 |

步骤 1 是确定拒绝的独立场景，不能套用到之前结果未知的请求。未知时保留原
路径、完整 body（含 expected）和 key，显式重放同一 POST；不换 key、改 body
或自动套用冲突提示。后一次拒绝或 head GET 不结算之前未知的结果。服务端
事件保存摘要和目标信息；完整原 POST 由调用方保留，没有 query-by-key 资源。

激活、retire 与 disable 共用部署范围的 binding 生命周期 key 命名空间，跨 binding、
revision、调用方与 credential label。先用 K 成功 retire/disable 后，再以 K 激活另一
仍然 published 的 revision，也返回 `409 idempotency_conflict`；先成功激活（包括
no-op）再用 K retire/disable 同样冲突。retire 与 disable 之间复用 K 也冲突。
冲突不增加事件或取消 intent，不推进 head/catalogue；原成功请求仍能原样重放。
确定拒绝未占用 K 时可由后续任一生命周期操作绑定 K，不能据此处理先前未知结果。

## AC5 附加解释与并发矩阵

初始 head=2，无第三方写入，两个不同新命令均 expected=2，目标均可激活。
表中顺序是持锁事务顺序；no-op 指 head 不变，仍有一次新事件。

| 第一 → 第二 | adapter 结果 | 最终 head | catalogue 增量 | 新事件数 |
| --- | --- | --- | --- | --- |
| 变更到 1 → 变更到 1 | 成功 / `configuration_changed` | 1 | 1 | 1 |
| no-op 2 → no-op 2 | 成功 / 成功 | 2 | 0 | 2 |
| no-op 2 → 变更到 1 | 成功 / 成功 | 1 | 1 | 2 |
| 变更到 1 → 原本的 no-op 2 | 成功 / `configuration_changed` | 1 | 1 | 1 |
| 变更到 1 → 原 key 同意图重放 | 成功 / 原结果 | 1 | 1 | 1 |
| no-op 2 → 原 key 同意图重放 | 成功 / 原结果 | 2 | 0 | 1 |

前四行分别覆盖无 key 与不同合法 key；最后两行必须使用相同 key/意图。
revision CAS 不含 epoch，不阻止其他写者把 head 改走又改回。no-op 与 replay
不计作第二次 head 变更；不能据此承诺“每次成功激活都消费一次机会”。

## 安全前置与执行入口

执行者先取得 SM 队列窗口与环境 owner 的明确回执：本任务独立且可销毁的
PostgreSQL 数据库/实例、版本、端口或 socket、管理员配置引用、资源限制、清理
责任和授权期限。配置仅通过 `SPEECHNEXUS_TEST_DATABASE_URL` 引用，不输出完整
DSN 或搜索其他任务/共享实例凭据。管理员需建删临时 schema/role、应用项目
Alembic 迁移、授予既有最小运行权限，并能用 `pg_stat_activity` /
`pg_blocking_pids` 观察本任务两个运行连接。运行角色不能是表 owner 或 DDL 管理员。
角色属于 PG 集群：独立数据库仍不足以隔离 CREATE/DROP ROLE，须使用环境 owner
确认的专用独立实例。角色认证须支持 fixture 的临时 LOGIN。

测试复用 `test_binding_lifecycle_pg.py` 的 sandbox：随机 namespace/role、正式
迁移、显式运行 grants、READ COMMITTED、独立连接与分阶段清理。fixture 先成功
CREATE 独占 schema，再执行迁移；只有成功创建的角色才登记删除。迁移、建角色、
grant、pool 创建/open 或测试体失败均尝试释放已取得资源；pool 先关闭，DROP OWNED
后删除本次 schema，再删除本次角色，最后关闭 admin/engine。名称冲突不接管或删除
已有资源；某项清理失败仍尝试剩余步骤并上报异常，不能保证权限/连接失效时零残留。
环境 owner 须按本次随机名称核查残留，不扩大清理范围。只能在已批准
隔离服务执行；缺前置时保持 not_run，不启动共享服务或改其配置。当前交付未
取得该能力卡/配置，因此下列入口只完成收集和静态检查，真实 PG 未运行。

在固定候选、独立锁定依赖、环境已核验后，从仓库根执行：

```bash
source "$SPEECHNEXUS_TOOLCHAIN_ENV"
test -n "$SPEECHNEXUS_TEST_DATABASE_URL" || exit 2
/opt/homebrew/share/delegra-managed/speechnexus/native-20261027-v1/bin/uv run --locked pytest -m integration \
  tests/integration/test_binding_lifecycle_concurrency_pg.py \
  tests/integration/test_binding_activation_semantics_pg.py \
  tests/integration/test_binding_lifecycle_fixture_pg.py \
  tests/integration/test_binding_lifecycle_pg.py -q
```

并发测试在释放第一事务前实际观察第二连接被该 PID 阻塞，观察不到则失败。
检查最终 head、catalogue、事件、不可变 publication 与 retirement，而非仅数成功
返回。这里直接调用 production adapter，成功不能记成 HTTP 200。
新增 HTTP 测试另经 ASGI 验证拒绝不占 key、原拒绝意图重评估、提交后冲突和
head 推进后的原结果。故障测试在真实 pool context 提交之后合成丢回执，再以
原完整请求/key 恢复；其合成故障不证明网络丢包、进程崩溃或 HA。

新增六种跨操作有序组合通过真实 PG/ASGI 检查同键冲突、原结果重放和状态不变。
`test_binding_lifecycle_fixture_pg.py` 在真实迁移提交后、建角色前、grant 和 pool
初始化注入失败，再用独立观察连接查询 schema/role 残留。这里的失败由测试注入，
不证明真实权限拒绝或连接中断。无数据库的故障注入覆盖另外的名称碰撞、pool open、
测试体和清理失败；其红绿结果仅证明清理控制流，不能替代该 PG 残留检查。

`make coordination-test` / `make integration-test` 还需隔离 MySQL/MinIO 与宿主
前置，可能启动受管服务；本单库窗口不授予这些运行资格。单元 SQL 模型、契约
校验与 mock transport 均不代验 PG。DEL-326 已指出 DEL-325 并发表“200”是
adapter 成功标记，HTTP 未运行；该发现与旧报告一并保留，交 SM 组织非作者复审。

dev4→dev5 沿用 wire 结构和现实现，但澄清旧文案可能被理解为更强保证的部分。
DEL-332 继续修订未发布 dev5 的共享作用域说明，以交接摘要固定候选并重新生成消费者。
调用方必须核对恢复策略；结构兼容检查、生成包成功或测试收集不等于消费者接入。
真实引擎停止、#273 AC3、设备、多机和投产 Gate 均另行取证。
