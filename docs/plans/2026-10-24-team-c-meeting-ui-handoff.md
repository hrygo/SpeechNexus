# 团队 C：会议记录与纪要工作台交接

核验日期：2026-10-24。base_sha=head_sha `7cd1887905dd57e8c647238416b6030049833e38`，分支 `codex/team-c-c00-versions`。未创建提交或推送。本单元建立在此前250文件的Meeting HTTP交付上；累计包校验以 `.runtime/team-c/team-c-current.manifest.json` 为准，完整 C00–C08 Goal保持active。

## 实际行为

- 正式“会议记录”入口消费现有项目/稿件分页、五项Meeting/Minutes操作；创建稿件关联会议、按编号打开、精确校对与追加封存、查看当前纪要。没有虚构会议列表或实时采集。
- 状态接口可选 `minutes_profile` 只公开批准的模板版本和模型名，不包含端点/prompt/binding/用量。缺字段、null或LLM关闭时禁止新纪要，基础记录与封存继续可用。
- Minutes请求可选 `expected_source_revision`；新页面始终发送当前显示封存版本。同事务检查新意图的源CAS；已接受同键同body先重放，省略新字段的旧意图hash不变。
- 同源有界传输复用原WorkbenchClient。类型/请求/响应由A既有流水线从C源契约生成，未手改生成产物。契约保持workbench `0.1.0.dev0` / bindings `0.1.0`，421条共享HTTP样例。需同版生产者/消费者接入，不作为混合版本部署资格。
- sessionStorage仅保存本标签页当前选择、草稿、完整原键/原body；请求先落盘再POST。网络/5xx/坏成功输出保留未知意图，LLM关闭仍可重放原接受请求。写成功后的GET失败同样保持unknown；只有写请求明确拒绝才能显式结束。
- 草稿基线固定到实际显示revision；新head需明确比较后采用，不能把旧正文自动套新版本。损坏/配额/其他写入者导致存储失败时保留可复制内存内容并阻断提交；离开页面不等于取消已发写入，迟到结果不覆盖新会话。
- 纪要只按文本显示；成功、失败、排队及过期与会议封存事实分开。旧202不冒充当前结果，随后读取GET事实；过期文字不展示，删除后保留本地草稿但阻断新写入。

## 验证与证据边界

- Python unit/contract：2882 passed，43.44s，无测试skip；format515、ruff、mypy382源文件和7项依赖边界通过。
- 28源契约与生成漂移通过；Web五门禁和1263项测试（contracts800+Admin79+Workbench384）通过。Node实测26.11.1与仓库声明26.12.1有差异，本单元未升级；Python3.14.9/pnpm12.14.0。
- PostgreSQL18.6一次性socket-only集群：`--browser --coverage` **295 passed /18.28s/94.42%**。本单元七个探针均独立核对pg_ctl退出3且无pid；未访问共享服务。
- Chromium156.0.8078.4加载生产bundle，连接真实C HTTP和上述隔离PG，LLM用fake：一个上下文/五次写请求；封存和纪要各一次实际提交后丢回执并原键恢复，纪要恢复时配置已关闭；纯文本、过期隐藏、删除与物理清理后本地草稿、360px/正文聚焦与单次Tab、quota后顶部功能切换保留内存草稿、零出站/零意外异常通过。两个503和一个404为明确测试注入/删除预期。
- 运行：先 `pnpm build`，再 `uv run python scripts/dev-env/workbench-pg-probe.py --pg-bin /path/to/postgresql/bin --browser --coverage --output .runtime/team-c/meeting-ui-probe.json`。`--browser`要求已有生产bundle和Chromium，缺失失败而不跳过；默认探针不运行浏览器。
- 详细记录：`.runtime/team-c/meeting-ui-7cd/`下RED/GREEN、门禁、PG清理、browser/result.json及meeting-360.png。测试使用合成文本，未加载模型、访问设备、执行生产根或变更共享配置。

## 审查与接入

本单元按[实施计划](2026-10-24-team-c-meeting-ui-plan.md)整体作唯一一次fresh终审：Critical0/Important2/Minor2；独立41项测试通过、37文件审查hash匹配。两项Important经一轮TDD修复：先session4+React1+App1失败，后54项针对性与Web1263通过，真实浏览器补quota后跨功能切换保留精确草稿。没有追加复审，修复由主代理检查与回归验证。

- I1已修：缺失/身份错误/未知新请求后失效旧纪要，界面不把未确认结果展示成当前成功，校对草稿和原键保留。
- I2已修：首次访问后的会议会话由App保留在隐藏页面中，返回重读配置和当前事实；配额失败时内存输入不因功能切换丢失。没有新轮询，关闭标签页/真实页面刷新仍需已落盘journal。
- Minor M1延期：读取失败后旧conflict仍可经useLatest重新设verified；服务端CAS/删除检查仍拒绝旧写入，未发现持久化绕过。
- Minor M2延期：真实host卸载后写后GET未中止，可继续无效读取；disposed守卫保住UI和durable pending，不将其当作取消。
- Declined D1：生产组合根/迁移/grants/联合删除仍由A串行实测，不把注入式app当生产接入。
- Declined D2：真实模型/设备/WS/音频/Linux/GPU和服务端出网限制缺证据，全部继续完整Goal。
- Declined D3：只读独立审查没有源码变异杀伤证据；两个Important有先红后绿，不代表全分支保护。
- Declined D4：已有正文focus/单次Tab和原生控件证据；完整键盘流程和读屏仍未验证。

A继续串行接生产组合根、owner迁移/grants和Meeting删除图/cleanup；必须一起接入，不能混用旧cleanup与新Meeting schema。本单元没有运行时出网限制或实际模型部署资格。

会议完整发现、实时WS/ASR、save_audio、要点/导出、真实LLM与网络/停止资格、删除页面、独立副本/导出、音色工作室、Linux/GPU及完整应用联合验收继续原Goal；不关闭父工作包或G0–G5。此前四项Minor继续保留：cursor细分错误描述、foreign-owner阶段说明、共享稿件组合并发回归和坏成功输出故障注入覆盖。
