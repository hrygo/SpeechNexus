# 团队 C C00 — 项目与稿件 HTTP 接入

核验日期：2026-10-24（Asia/Shanghai）。`base_sha=head_sha=e0d8992aa7de98b22c5fee91cf33d3da5f646f8d`，分支 `codex/team-c-c00-versions`。安全吸收#75/#76，204旧C文件中202文件字节未变，两份README保留双方内容。无C提交/push，完整C00–C08 Goal保持active。

## 已实现边界

- `create_workbench_app` / `create_version_router` 注入 `WorkbenchVersions`、部署默认workspace、显式 `max_json_bytes` 和 `body_timeout_seconds`；无开发fake自动装入服务。生产共享组合根和迁移仍由A串行集成，此工厂没有自动启用生产workbench。
- 七个实际operation：createProject/getProject/updateProject/createTranscript/getTranscript/createTranscriptRevision/getTranscriptRevision。路径、方法、请求和响应校验来自A既有生成入口；OpenAPI仅展示实际注册的七项操作，JSON DTO仍来自权威契约。
- bounded JSON intake拒绝重复JSON key/语义header/query、非法UTF-8/孤立surrogate/非有限数字、超大/超时/非JSON写入；校验在业务事务前。错误只含固定代码和消息、request_id，不含正文、原键、SQLSTATE、路径或内部异常。HTTP取消继续向ASGI host传播，不自动重试。
- Project/Transcript原键重放、CAS、空校对和精确Unicode保留；speech来源调用现有公开SourceReader，HTTP/解码不占PG事务。历史View使用当前metadata和指定immutable revision，PG以一条语句同时读取head/选定revision及parent/child tombstone可见性。
- C-owned workbench契约补入口400/408/413/415、依赖503、异常500、来源410、删除后create原键重放404；代码语义写入source描述。仅运行既有生成器，未修改生成逻辑、共享锁文件或speech契约。`VersionStoreError`移至应用port，原PG导入路径显式re-export，原属性/消息保留，interfaces不反向依赖infrastructure。
- 明确关闭FastAPI默认Swagger/ReDoc及OAuth辅助页面，保留同源机器OpenAPI，避免默认文档引入公网CDN脚本/样式/字体/图标。

## 验证与范围

| 验证 | 本轮实际结果 |
| --- | --- |
| 原样 `uv run --locked pytest tests/unit tests/contract -q` | 2205 passed，28.08s，无skip |
| HTTP针对性测试/语句分支coverage | 37 passed，96.03%（http_ingress + http_versions） |
| socket-only PG probe `--coverage` | 265 passed，8.08s，无skip，组合coverage93.80% |
| 真实网络 | 上述PG中Uvicorn绑定127.0.0.1随机端口/httpx trust_env=false，创建/原键重放/并发CAS/历史读取/删除后不可见及原键不可复活 |
| PG一致读取 | 单语句断言、并发修订/读current和历史、缺失revision拒绝 |
| PG停止 | `.runtime/team-c/pg-o9ljie6m` stopped=true，另核对pg_ctl状态3及无postmaster.pid |
| Python静态 | format453、ruff、mypy335、7项依赖检查通过 |
| 契约 | 28个源文件验证与生成drift通过；HTTP共享正负fixtures140→222，Python/TS同版消费 |
| Web | format/lint/typecheck/test/build通过，815=contracts450+Admin79+Workbench286 |

证据在本机忽略目录 `.runtime/team-c/http-e0d/`，不是已发布到main的远端制品。一次独立终审Critical0/Important1/Minor1：Important为默认docs公网CDN，新增测试实测200的RED后关闭页面，修后37项及全库/PG通过，不再次复审；仅http_versions.py及其测试相对审查hash变化，其余14文件原样。Minor为405 Allow只列首个GET、漏PATCH，合法PATCH仍可调用，已记录延后。本机uv Python3.14.9、PostgreSQL18.6、Node26.11.1、pnpm12.14.0；Node与.nvmrc26.12.1差异保留，未自行升级。旧737/2874/a98交接保留原时点事实；本单元未运行新的真实浏览器/物理设备/模型/GPU/Linux/恢复/内容出站资格验证，未把既有合成浏览器证据扩大为新HTTP UI闭环。

## TDD与未完成项

新增HTTP错误状态先36项RED（已有400正例4项通过），修复source并生成后GREEN。HTTP模块不存在RED，接入后历史query因缺一致读取仍失败，再接port/单SQL后GREEN。真实PG新增删除原键重放先500/未声明404 RED，补权威source后取得404；不绕过validator或回传旧内容。错误method另有RED→GREEN。首次PG删确认fixture少传owned child，被既有确认屏障拒绝，修正为完整confirmed_targets；未放宽生产规则。

隔离进程mutation移除生成请求校验后，非法path/query四例中3项失败、重复query仍由ingress拒绝；审查者另移除重复JSON key guard后捕获1项失败。两者不修改工作区源码，证明测试实际覆盖这些入口。结构图谱覆盖复核仍project not found，具名源码回退；未创建索引。

本单元不覆盖status/List分页/删除HTTP路由、Meeting/Minutes、公开realtime、工作台校对页面与下载协议、生产root/迁移和真实语音闭环。后续在已交付C1入口上继续；C2/C3按具名版本接入，不等待A整包关闭。C07领域核心保持原字节，其持久化/真实保存副本/质量确认/导出仍待完成。所有C issue及Gate保持open/未通过，C03仍planned。

收尾只读核对PR元数据：C2 [PR #77](https://github.com/hrygo/SpeechNexus/pull/77)仍open/未合并，最新head `9a913ae9e94b4a8961c476bf6ca89b05fad7b019`，基线已含B VAD；C3按A00 #19实施中。CPU VAD [PR #78](https://github.com/hrygo/SpeechNexus/pull/78)已合入main `3b8052040111cdd8af3a50aaf5b0e8b3922e1f42`。B向C04 #39追加的独立交接块已识别并完整保留，不覆盖；其16项不适用skip不算通过，不扩大为实时/设备/Gate资格。本单元固定e0d，不把这些尚未由C本地吸收的制品计入本轮测试，后续按具名SHA集成。未发送新跨chat消息。

累计e0d补丁与逐文件manifest由本轮收尾生成，位置 `.runtime/team-c/team-c-current.patch` 和相邻 `.manifest.json`；安装前检查全部旧文件保护、干净e0d apply、全部字节、本地链接和真实index不变。旧204文件737包de342b31保留；最终校验值以manifest与issue回读为准，不把本机包当作远端合入。
