# 团队 C 公开实时 ASR 消费与字幕页面交接

核验日期：2026-10-24（Asia/Shanghai）。base_sha=head_sha `9eea85987232eaebbcbd6768c46fe0a084a4d177`；未提交或推送。本文件只交接 ASR 消费者，不表示 C04、C00–C08 或 G0–G5 完成。

## 实现与边界

按[实施计划](2026-10-24-team-c-realtime-asr-plan.md)消费同版生成 REST/事件解析入口。正式工作台新增实时字幕：填写部署实际发布的 profile/model `{id, revision}`，显式创建、连接、开启麦克风、提交段、排空/中止及查询当前事实。引用初值为空，无虚构模型或 ready。

`web/workbench/src/realtime/` 分为 ASR reducer、固定同源客户端、闭合恢复 journal、设备/WS controller 与 React 展示。24kHz mono PCM16LE；单帧不超过100ms及协商上限，JSON至多64KiB、输入credit至多2s、网络积压5s（含Base64/JSON）、本地捕获队列至多1s、等待credit至多1s。ACK credit保守扣除确认水位之后已发送的音频；硬边界失败停止输入，不补静音。HTTP为15s总超时、JSON8MiB上界；失败不自动重试。

先有 `session.created` 和匹配 `nexus.session.ready` 才能采集；恢复标签页不自动连接/开设备。断线、隐藏、导航及卸载释放本次设备/连接，明确gap；新会话不重放旧PCM。drain停止本地输入，保持WS接收服务排空结果，通过原键HTTP请求结束；abort关闭连接并使用原键HTTP，接受结束请求不表示算力释放。

预览完整替换，最终正文（含空成功）不可被迟到预览或required辅助失败覆盖。item/segment交错关联合并为同一段；Operation终态与清理单列。metadata只显示版本待核验，不猜测completed未声明的text_revision映射；公开规则须A明确后再挂分人/对齐。

sessionStorage保留原创建/取消key、完整原请求、原回执与有界最终正文草稿；每次首次/重放均重新CAS核验，损坏、外部改写及quota阻断网络。正文最多4MiB/10000段，不持久化PCM、preview或凭据；存储失败保留内存正文，显式重试保存。当前GET与原接受快照分开，读失败隐藏当前事实。草稿和显式UTF-8文本下载不表示已保存Meeting/Artifact；多个会话边界单独显示。

## 验证

- TDD：10条共享语义序列、credit/ACK/epoch、预览/空final/required失败、item交错、原键及存储CAS/quota、晚到设备/GET、drain与导航，最终41 focused通过。真实浏览器发现的采集/协议错误被取消回执清除问题，已补RED并修复，停止原因继续显示。
- `node scripts/dev-env/workbench-realtime-asr-probe.mjs --output .runtime/team-c/realtime-asr-9ee/browser-fixed`：Chromium156.0.8078.4、生产bundle、loopback合成HTTP/WS、合成麦克风，最终3会话/10写请求；创建/drain/升级abort各实际接受后截断回执与原键重放、外部存储改写阻断、credit耗尽无超发、drain保持连接、双击提交不误中止、两段final反序后按输入顺序保留/下载、预览替换/精确Unicode正文/空final/required失败、metadata待核验、导航设备释放、360px与基础focus/Tab、0出站/意外异常/合成server异常。
- Web五门禁最终1325=contracts805+Admin79+Workbench441；28源契约/生成漂移通过。Python生产/测试与所有生成物本轮未改；上一单元Python3006、PG296及会议/删除真实C HTTP证据仅按仍有效的范围复用，不当作本轮新跑或公开实时生产服务资格。
- 当前仓库未索引，结构工具coverage返回无当前项目；具名源码与生成契约直接核验，不重建索引或声称完整图覆盖。

## 仍未验证与接收方

A05/B04公开生产realtime、真实ASR模型/设备/网络隔离/停止资格、Linux/GPU和跨浏览器/完整键盘读屏未验；合成服务不进入生产路由或遥测。A负责公共session/admission/Operation/receipt服务和metadata精确正文版本语义；B负责流式引擎、VAD及资源释放事实。无新跨chat消息；依赖清单仅在本机 `.runtime/team-c/realtime-asr-9ee/producer-dependencies.md`。

TTS/双工、实时正文进入Meeting/Transcript及save_audio、完整辅助绑定、独立副本/A07恢复和其余工作包继续完整Goal。旧六Minor保留待办。

## 唯一终审与一轮修复

唯一fresh终审22文件始末hash一致；Critical0/Important3/Minor1。主代理仅一轮TDD修复I1–I3，先4 failed/37 passed，后41 passed及Web1325/浏览器重新验证；没有追加复审。

- I1：快照公开未提交samples，UI与controller据此阻止空提交，额外committing屏障覆盖异步队列内双击；空提交不触发整场abort。
- I2：按committed.previous_item_id与已知sample_span维护有界输入顺序，延迟前驱可补齐、矛盾环拒绝；最终草稿按此顺序重建，保留旧会话边界，空final/重复final/刷新和下载顺序一致。
- I3：区分重复drain和已接受drain后的显式abort，后者先保存独立原键/原body再HTTP；未知drain原pending不覆盖，abort丢回执恢复用自己的原键，cleanup仍待GET确认。
- Minor M1延期：网络容量拒绝/send失败时，reducer先计入的sent可能包含该未发送帧，UI“已发送”偏高；accepted不虚增、输入失败会停止。需后续明确发送提交语义及回归，不用ACK替代实际发送。与旧六Minor共七项待办。

7项Declined的Final裁决：metadata映射维持待核验；HTTP合法关闭入口不额外双写WS；生产者/Meeting保存维持依赖边界；旧seq忽略不改写事实且512缓存不代表全生命周期审计；恢复不自动连接/采集；未变Python/PG按原证据范围复用；不为行数作无收益拆分。理由及代价逐项保存在本机ledger，不扩大验证资格。

累计295文件；最终补丁与SHA以 `.runtime/team-c/team-c-current.manifest.json` 为准，旧279/74bef065包保留 `.runtime/team-c/team-c-before-realtime-asr.*`。干净9ee应用、全部字节/363本地链接、真实index保护及九issue逐字回读的精确证据保存在本机本单元runtime收尾记录。最终源码与初审22文件相比的变更仅为一轮修复及交接收尾，具名差异由manifest记录；无commit/push或新跨chat消息。
