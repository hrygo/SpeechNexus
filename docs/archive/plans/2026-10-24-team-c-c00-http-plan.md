# C00 项目与稿件 HTTP 接入 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans inline, with one fresh final review. 不提交/push，不重复审查历史C单元。

**Goal:** 用生成HTTP校验提供真实项目/稿件创建、修改和固定版本读取路由，保持原键重放、来源边界和无内容错误。

**Architecture:** C-owned FastAPI router/app factory注入WorkbenchVersions和部署默认workspace。只注册实际实现的七个operation；共享生产组合根仍由A串行装配。工作台版本查询通过单语句PG快照读取当前metadata与指定历史revision。

**Tech Stack:** 既有FastAPI/httpx、Python3.14、PostgreSQL18、A生成的workbench-http Python/TypeScript验证器。

**Spec:** `contracts/workbench-openapi.yaml`、`docs/architecture/technical-blueprint.md` 工作台边界、C00 #35/C08 #43、完整C00–C08 Goal。

## Global Constraints

- base/head `e0d8992aa7de98b22c5fee91cf33d3da5f646f8d`；204旧C文件以http-e0d/result.json保护；保留README双方内容。
- 不导入speech内部、不共写底座表、不访问引擎；无登录/ACL/个人空间。无新依赖/锁文件、生产root/迁移写入。
- 先改C-owned权威契约并运行既有生成流水线，不手改生成产物或改生成器；新增错误状态不改变成功DTO。
- JSON接收限制字节和时间；拒绝重复参数/JSON key、非法UTF8/非有限数字/非JSON媒体/无契约字段；不回显内容或异常。
- 响应必须经过生成HTTP校验，未声明错误不能伪装404/成功；提交结果未知不自动重试、不声称回滚。
- 不提交/push或发新跨chat消息；只用合成数据和本项目隔离socket-only PG/loopback。

## Review Focus

1. 原键提交成功后丢失响应：重放原快照，不能隐式再生成或返回虚假失败事实。
2. 历史稿件读与修订/删除并发：当前metadata与选定revision同一语句快照；不返回跨稿来源。
3. 超大、分块、慢请求及重复header/query/JSON key：事务前拒绝，错误不携带正文/键。
4. 生成请求和响应边界都实际执行；Unicode/header不可安全回送时使用服务器request_id。
5. 未启用服务、来源过期、store故障和生产者非法响应：稳定状态/错误且无异常泄漏，取消向上传递。

### Task 1: 冻结可执行错误响应

**Files:** C-owned `contracts/workbench-openapi.yaml`，既有生成产物，新增 `tests/contract/test_workbench_http_errors.py`。
**Interfaces:** 消费既有 `validate_response(operation,status,{body,headers})`；七个operation补400/408/413/500/503，写操作补415，导入补410。

- [x] 写参数化错误正负用例，观察未声明状态RED。
- [x] 只补对应source responses、入口错误语义，运行 `make contracts-generate`，同一测试GREEN；生成器不改。
- [x] `make contracts-check contracts-drift`，Python/TS共享fixture消费者验证。

### Task 2: 路由和一致版本读取

**Files:** 新增 `workbench/interfaces/http_versions.py`、`http_ingress.py`；修改 `application/versions.py`、`version_ports.py`、`infrastructure/postgres_versions.py` 与 `tests/fake/workbench_versions.py`；新增 `tests/unit/backend/test_workbench_http_versions.py`。
**Interfaces:** `create_workbench_app(service: WorkbenchVersions|None, *, default_workspace_id: str, max_json_bytes: int, body_timeout_seconds: float)`；`create_version_router(...)`供A注入；`get_transcript_view(id,revision=None)`返回domain二元组。

- [x] 先写完整HTTP项目→原键重放→稿件→CAS→历史查询，以及非法/来源失败/取消/响应错误用例，确认RED。
- [x] 实现七个operation、限界JSON接收、稳定错误映射、请求ID与生成OpenAPI实际路径投影，GET历史走一致读取。
- [x] 核对原键不变、历史正文不覆盖head、失败前无持久化、无诊断或正文泄漏；GREEN。

### Task 3: 实际网络/PG与交接

**Files:** 新增 `tests/integration/test_workbench_http_postgres.py`，更新隔离PG probe；handoff/goal/index与累计包。

- [x] 实际Uvicorn loopback+httpx请求，真实隔离PG创建/重放/CAS/历史/删除不可见；观察可复现RED后GREEN。
- [x] 按风险运行Python静态/依赖/原样unit+contract、隔离PG、契约及Web消费者门禁；明确不证明真实语音/设备/Linux/GPU资格。
- [x] 一次fresh sol_worker final review；Important/Critical一轮TDD修复，Minor只记录，不复审。
- [x] 累计补丁干净e0d apply/全部字节/index保护；更新并回读issue正文任务，完整Goal active。
