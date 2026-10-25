# 团队C：实时TTS消费者与播放交接

日期：2026-10-24。base/head：`9eea85987232eaebbcbd6768c46fe0a084a4d177`。未提交、推送或发布。当前补丁与逐文件哈希以本机 `.runtime/team-c/team-c-current.manifest.json` 为准。

正式工作台新增「实时配音」入口，按[实施计划](2026-10-24-team-c-realtime-tts-plan.md)消费既有生成REST与RealtimeEvent。部署profile/model/voice引用无虚构初值，voice可由服务端解析但必须在接受回执固定；同源连接、显式开始一轮、追加/冻结/finish/cancel与drain/abort、原键恢复和独立GET清理事实。ASR消费者与其严格journal继续保留。

文本seq连续且一次最多一个未确认append，text_credit保守按UTF-8字节计算。文本冻结后不修改；重复语义事件按规范化SHA-256去重，错epoch/utterance/operation、音频区间/序号缺口和非法PCM拒绝。网络PCM只从receive返回进入有界播放，不进入React快照或journal。Operation成功、收到音频与Worklet实际渲染水位独立显示，物理扬声器延迟不在此证明范围。

AudioContext从明确用户动作resume，复用既有AudioWorklet/PlaybackChannel，PCM时钟固定24kHz、mono PCM16LE；本地队列最多120000样本，消息64KiB及在途消息/字节各有上界。隐藏、导航、异常、取消和迟到初始化停止本地资源；取消回执不冒充资源释放。断开不自动重连/音频重放；文本、REST原key/body和会话元数据通过每次dispatch的sessionStorage CAS屏障恢复。真实卸载只清本地资源，未确认命令由原键/GET恢复。

## 当前验证证据

- TDD覆盖生成请求/引用、文本credit/冻结、命令发送失败不推进水位、规范化重复事件、PCM区间/解码、终态、原键/外部改写、播放启动迟到、取消/导航、真实页面纯文本和恢复错误。相关realtime/App共78项通过（含原ASR回归）；新TTS29项另有共享REST/App测试。
- Web测试1355＝contracts805＋Admin79＋Workbench471；完整门禁与终审修复后的最终数量以本单元runtime为准，不从早期结果推断后续源码。
- `make contracts-check` 28源文件通过，`make contracts-drift`生成物一致；Python/PG源码与生成产物未改，原Python3006/PG296仅复用其原范围证据，非本轮新跑。
- `node scripts/dev-env/workbench-realtime-tts-probe.mjs --output .runtime/team-c/realtime-tts-9ee/browser-fixed`：生产bundle、Chromium156.0.8078.4、合成REST/WS/PCM与实际AudioWorklet，3轮utterance，其中2轮各48000样本收到并渲染，合计96000样本；中间一轮在started前取消。生成done时渲染尚未完成、重复音频不重播、创建/关闭接受后截断回执原键恢复、刷新无自动播放、取消/导航context关闭、精确Unicode/纯文本、hidden→visible的DOM故障注入后仅显式重放、ACK文本在drain后仍可冻结/finish、360px及基础Shift+Tab，出站/意外异常/合成server异常为0。后续修复的最终probe路径以runtime为准。
- 当前仓库无可用graph索引，coverage核对后直接读取具名源码；未重建索引，不声称完整调用图覆盖。

## 范围与接收方

A05/B04公开生产realtime、流式引擎和停止/释放资格仍未交付；合成producer仅在test-only脚本，不进入生产路由。A负责生产会话/Operation/receipt与固定默认声音事实，B负责真实流式与资源停止。客户端输出ACK未在契约声明，未新增该消息；本地有界队列不代表服务端收到播放水位。

双工、助手turn、音色工作室、实时正文/音频进入Meeting、独立副本/导出/A07恢复、真实模型/物理设备/网络/Linux/GPU与完整跨浏览器/读屏资格仍待完成。七项既有Minor原范围保留；完整Goal active，C00–C08/G0–G5未关闭。没有新跨chat消息或生产运行态操作。

本单元最终审查、裁决、延期项、精确验证和包应用/链接/index保护证据保存在本机 `.runtime/team-c/realtime-tts-9ee/`；仅按实际证据更新issue，不把分段交付当整包完成。

## 唯一终审与一轮修复

独立终审21文件始末hash匹配，Critical0/Important3/Minor1。三项Important经6个失败回归复现（6 failed/20 passed），一轮修复后相关78/Web1355通过，完整门禁和增强后的browser-fixed重跑；没有追加复审。

- I1：start已送出、started未到时，按utterance发送cancel；迟到started保留cancelling，迟到PCM不播放。
- I2：文档重新可见仅恢复显式操作资格与当前GET；不自动连接/开始或重放。功能active与可见状态共同决定操作许可。DOM故障注入不等于真实后台节流/休眠资格。
- I3：drain立即发布phase，只禁止新start/append；原utterance可以处理迟到ACK、commit/finish/cancel。实际浏览器验证接受文本在REST drain原键恢复后完成生成与播放。
- Minor M1延期：done先到、缓冲仍在播放时，本轮停止按钮禁用；整会话abort仍可静音，但会失去session复用。与旧七项Minor共八项待办。

九项Declined均逐项裁决记录在runtime ledger：生产执行/释放、模型质量/空输出判定、物理设备/跨平台、完整a11y、长时负载、完整图谱、旧包七Minor、mutation及修复后独立审查，均未据局部证据宣称完整资格。修复后的受审源码差异按manifest具名记录；所有未涉旧文件以hash保护。
