---
title: "团队 C C00 文本字符边界交接"
status: active
date: 2026-10-24
---

# 团队 C C00 文本字符边界交接

本单元补齐[版本单元](2026-10-24-team-c-c00-version-handoff.md)与[原始转写导入](2026-10-24-team-c-c02-transcript-import-handoff.md)之间的文本校验差异，不代表 C00 或团队 C 全部完成。

## 基线与行为

本轮先核对并快进到 `ecd70607482c451ff8dcfff8024e80bc42aefab5`（B01 持久化提交账本）；其文件范围与原有 C 交付的 90 个文件不重叠。使用 `uv sync --locked --all-packages --all-groups` 同步更新后的锁定环境。`base_sha` 与 `head_sha` 均为上述 SHA，本地 C 改动未提交、未推送。

实测旧领域层接受 NUL 和孤立 high/low surrogate，随后 psycopg 分别抛出 `DataError` / `UnicodeEncodeError`。现在统一在领域文本校验中拒绝这些字符，以不含正文的 `InvalidVersionChangeError` 返回。覆盖项目创建与编辑的名称/描述、稿件标题、手工原文、追加校对；原有长度、空正文、CAS、封存和历史重放语义保持不变。

文本不进行替换、裁剪或 Unicode 归一化。合法中文、emoji、组合字符、换行、空白及可存储的控制字符原样保留。已有导入适配器仍将来源文本问题报告为 `source_invalid`；领域层也保护其他注入 reader 的返回值。

生产改动仅为 `backend/src/speechnexus_backend/workbench/domain/versions.py` 的共有文本规则；测试位于现有领域、用例和 PostgreSQL 测试文件。没有 DDL、数据迁移或底座状态变更。A 的生成流水线、组合根和公开协议文件没有改动。

## 验证与证据

核验日期：2026-10-24（Asia/Shanghai），本机 Python 3.14 / PostgreSQL 18.6。样例均为合成文本。

- RED：领域拒绝测试 21 失败；fake 用例 3 失败；隔离 PostgreSQL 探针 3 失败、99 通过，确认失败来自当前驱动的字符写入错误。
- GREEN：针对领域/用例/请求边界/导入的测试 93 通过；完整 `uv run pytest tests/unit tests/contract -q` 为 805 通过。
- 隔离探针 102 通过：63 个真实 PostgreSQL 测试（其中 6 个真实 loopback HTTP + PG 导入用例）、4 个清理宿主 fake 测试、35 个来源适配器单元测试。基础设施 statement/branch 合并覆盖率 91.92%。新增 PG 用例经 A 的项目请求 parser 进入领域层，验证所有文本字段拒绝、失败后同键重试、版本/回执无残留及合法 Unicode 往返。
- `uv run ruff format --check .`（265 文件）、`uv run ruff check .`、`uv run mypy`（190 source files）、`uv run lint-imports`（7 条）、`make contracts-check`（28 文件）及 `make contracts-drift` 均通过。
- 进程内变异：绕过 NUL 检查使 7 项拒绝测试失败；允许 surrogate 编码使 14 项拒绝测试失败。原源码字节未改动。按正确性、可读性、依赖方向、正文保密及有界输入审阅本单元 diff，未发现待修复项。

探针通过以下命令新建并停止项目专用、禁用 TCP 的一次性集群，不接触同机共享 PostgreSQL：

```bash
uv run python scripts/dev-env/workbench-pg-probe.py --pg-bin /path/to/postgresql/bin --coverage --output .runtime/team-c/c00-text-pg-green.json
```

本机证据：`.runtime/team-c/c00-text-unit-contract-final.log`、`c00-text-pg-red.json`、`c00-text-pg-green.json`、`c00-text-nul-mutant.log`、`c00-text-unicode-mutant.log`。RED/GREEN 集群分别为 `pg-cd0rb27v` / `pg-072x2mg_`，均已确认停止。

累计可恢复交接包仍为 `.runtime/team-c/team-c-current.patch` 与相邻 manifest，后者记录当前 base、文件摘要和补丁 SHA256；本轮刷新后包含新增交接文档及完整 C 本地实现。旧补丁与 manifest 另存为 `team-c-c02-import-cbf4a8c.*`，保留原核验记录。

## 未验证项与下一接入

bindings/fixture 版本仍为 `0.1.0`，speech/workbench OpenAPI 的 `info.version` 为 `0.1.0.dev0`；这些文件内容与上一单元相同；本单元没有新增公开请求或响应形状。工作台 HTTP 的领域错误映射、完整生成入口、对象区、字幕/导出协议与生产组合根仍待接入。本轮没有重跑 Web/浏览器/物理设备/GPU/恢复验证，不能据此宣称生产资格通过。

下一步消费 A 提供的同版项目/稿件响应、创建/校对请求与实时事件 parser，接公开 HTTP 和实时消费者；缺少入口时继续 C 所有的业务实现与契约方案，不复制生成 DTO 或假定生产闭环可用。[完整目标](2026-10-24-team-c-goal.md)保持 active。
