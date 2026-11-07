# 同步语音合成：持久命令与恢复

当前公开 `POST /v1/audio/speech` 拒绝 `Idempotency-Key`，内部原身份恢复不等于客户端可以
安全重发同一 HTTP 请求。`backend_unavailable` 和 `operation_timeout` 虽标记为
`retryable: true`，也不能据此认定重发不会创建新执行；调用方须检查 SDK 和代理的自动重试，
结果未知时由有权操作者协调原身份。SDK 示例见[兼容边界](../architecture/openai-compatibility.md#2-官方-sdk-调用)。

公开请求可在 `nexus` 中指定 `max_output_audio_ms`，为 100–3,600,000 ms 的整数；省略时沿用部署上限。示例：

```json
{"model":"tts-quality","voice":"voice-demo","input":"Synthetic speech.","response_format":"wav","nexus":{"max_output_audio_ms":100}}
```

有效值只收紧部署策略，源 PCM 上限为 `floor(ms * sample_rate_hz / 1000)` 个单声道 PCM16 样本；编码制品另受字节上限限制。MP3 编码帧或填充不能放宽源时钟限制。后端发现超限时返回 503 `speech_output_limit_exceeded`；网关拒绝/无效结果沿用既有错误，均不截断、部分返回或自动重新合成。非法类型、范围、空值与未知字段在执行外发前返回 400 `invalid_request`。请求上限高于部署值时使用更严格的部署值；网关仍拒绝不符合固定 binding 的命令。新字段须先部署支持服务端再启用客户端；旧服务端会明确拒绝，客户端不得删字段重试。

同步 TTS 适配器使用正式 admission 事务，将完整生效参数、固定声音版本、网关请求和原始身份作为私有 `business_command` 与租约、Job、outbox 一起提交。提交完成后才可调用网关。恢复从持久命令重建同一请求，不读取当前声音、绑定、时长上限或语言默认值；提交结果未知时查询原身份，网关确认该身份不存在时才使用原请求提交。取消、断线、超时或任务终态均不构成停止和资源释放证据。

恢复调用须显式提供读取与跟随的时间预算；它与原执行截止时间独立，不能延长持久请求中的截止时间。预算从进入恢复时起覆盖原命令读取；超时后后台读取可能结束，但不得因此外发执行或释放预算。音频回执的请求摘要须匹配完整持久网关请求（含派生输出流和规范化日期）。

新准入与声音退役使用同一 voice catalog 事务锁；在该锁后重新检查精确发布版本、退役记录及内建声音可信资格。已有原身份的重放先于当前资格检查。启用 binding 禁用屏障时，先取 binding、voice 锁，再按 resource pool、workspace quota、host、runtime instance head、instance 和相关 runtime intent 顺序锁定准入父事实，随后处理 operation、attempt 与 lease。资格校验缺失时新准入失败。部署角色须具备声音发布记录、退役记录和资格证据的必要只读权限。

`SpeechPlan` 固定声音的数字 revision 和 revision ID。公开请求未指定语言时，只有明确提供的部署默认语言可用于首次准入，并随命令固定；未配置时拒绝准入。执行网关的 file TTS 要求非空语言且文本不能超过固定 planner 上限，适配器在准入前检查这些条件。

`build_speech` 在配置完整且资格有效时启动 API 生命周期内的 settlement worker；结算循环启动失败则工厂不启用。启用须显式配置私有准入配方、默认语言、执行网关和对象存储凭据，检查当前实例与独立声音资格；缺少配置或有效资格时保持不可用。配方只声明部署分配，不授予资格。请求解析使用已发布、未退役且匹配当前实例的声音，准入事务在声锁内校验资格和实际参数，并在预算与结算写入的等待结束后、提交前再次核对有效期；已有原身份重放保留历史授权；应用关闭时释放该链路持有的连接池与客户端。

配置键为 `SPEECHNEXUS_SPEECH_RECIPE_FILE`、`SPEECHNEXUS_SPEECH_DEFAULT_LANGUAGE`、独立身份的 `SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL` 和既有执行网关及对象存储配置；配方必须是受限权限的私有文件。运行角色需要既有协调事务权限及 `speech_synthesis_reader_grants` 声明的读权限及 settlement 根写入权限，部署阶段负责配置；缺失资格时工厂不会轮询自动开启，新增资格后须重启进程。

准入同事务写入不含内容的 settlement 根；取消意图与 admission 按 operation 锁关联并固定首次原因，后台扫描原身份，由 LeaseService 核验停止证据后释放。该链路已接入应用生命周期；API 停止或工厂未启用时，内嵌循环不会继续处理待结算记录。独立入口 `python -m speechnexus_backend.tts_settlement_worker` 可按部署配置运行，要求独立数据库身份和明确的网关、节点，不能仅凭 API 停止推断结算完成。资格的网关策略摘要与 native speaker 随私有命令固定，首次提交或 404 重放前与认证网关现场配置比对；已存在的提交继续按原身份恢复。此检查依赖运行窗口内同一 gateway、instance 和 generation 使用同一进程中不可变的配置快照；网关重启或复用同一身份更换配置属于部署边界，须重新取得资格。不能以合成测试替代真实引擎停止、多机或高可用验证。

正式原身份恢复入口为 `python -m speechnexus_backend.recover_tts REQUEST_ID --timeout-seconds N --output-file /absolute/private/path`，使用独立配置的数据库、执行网关及版本化对象存储连接，不读取当前配方、声音资格或语言默认值。`REQUEST_ID` 是原私有命令中的服务端生成身份，可由有权检查原 intent 的操作者按 operation 定位；它不是客户端提供的追踪头。命令将音频写入用户指定的绝对路径；父目录须归当前用户且仅其可访问，文件以 0600 创建且不覆盖，标准输出仅含脱敏摘要。恢复调用预算从入口计时，不延长原执行截止时间；执行已存在时按原身份读取结果，404 重放仍须现场证据匹配原固定策略。

启动前对未释放的原 TTS 与资格探测租约进行有界只读检查；如升级前任务缺少对应 settlement 根，拒绝启动并要求按原身份协调，不自动创建新执行或批量补写。检查同时覆盖公开 asr/tts：这类租约不需要 settlement 根，由派发器在终态释放；冻结期限过后再留 60 秒结算窗口，仍被持有且无根时拒绝启动。此检查不替代已有集群恢复与生产数据迁移演练。
