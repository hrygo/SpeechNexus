# 集成测试目录规则

继承[根规则](../../AGENTS.md)。执行前读取 [README.md](README.md) 的隔离配置与证据范围；协调逻辑变更使用 [speechnexus-coordination-change](../../.agents/skills/speechnexus-coordination-change/SKILL.md)。

- 只连接本项目隔离的测试服务，使用合成或最小脱敏数据。测试管理员可以创建和清理独立 namespace、角色、用户、桶；产品运行角色不因此获得管理员或 DDL 权限。
- `make coordination-test` 与 `make integration-test` 必须同时设置 `SPEECHNEXUS_TEST_DATABASE_URL`、`SPEECHNEXUS_TEST_MYSQL_URL`。后者还要求隔离 MinIO endpoint 与测试密钥；完整变量见[验证指南](../../docs/agent-verification.md)。
- 直接运行共享事务 pytest 时，两个 DSN 都缺失会跳过，只配置一个会失败。单库通过或跳过不能报为双库验收；缺配置时保留门禁失败并报告条件。
- PostgreSQL 是正式产品目标；InnoDB 验证关键协调路径，不能推导为全产品 MySQL 支持。共用行为 oracle 的引擎映射遵循测试说明，不能为通过某库而放宽业务语义。
- 并发证据需要独立连接和可观察的锁等待/交错，不能用 sleep 或顺序调用替代。双库测试中的合成停止事实不能证明真实引擎停止，丢弃响应不能证明真实网络故障或 HA。
- `tests/support/` 原型不能供产品导入。历史 PG 门禁与当前受控事务各有保护边界；按 [ADR 0007](../../docs/decisions/0007-legacy-coordination-retirement.md) 迁移有效不变量、核对退出条件后移除废弃门禁，不永久并养旧原型。

Make 目标可能启动受管服务。先核对实际宿主、服务、端口与当前授权；缺少条件时报告未验证范围，不改向共享实例。具体命令和运行态说明统一见[验证指南](../../docs/agent-verification.md)。
