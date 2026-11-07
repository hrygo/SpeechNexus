# 文件 CustomVoice 固定输入

本目录保存固定制品、vLLM-Omni 镜像/源码及完整草稿注册定义；不保存模型、本机路径、
凭据、binding revision、容量 manifest 或资格回执。

离线校验复用现有命令，其名称为兼容 ASR 调用方保留：

```bash
uv run python scripts/dev-env/verify-file-asr-artifacts.py \
  --pin deploy/engines/file-tts-customvoice/artifact-pin.json \
  --model-root "$SPEECHNEXUS_TTS_MODEL_ROOT"
```

生成现有 `BindingDefaultsCreateRequest` 时，另加
`--emit-binding-request --binding-definition deploy/engines/file-tts-customvoice/binding-definition.json`，
以及注册方提供的 `--binding-id`、`--logical-model`、`--capacity-manifest-id`。
不传 `--vad-file`；工具不发 HTTP、不写数据库或分配身份。

详见[制品、参数与部署交接](../../../docs/operations/file-tts-binding-handoff.md)。
`runtime-pin.json` 是本次镜像只读核验的固定记录，离线模型校验不重新证明运行容器或依赖。

## 节点模板与变体槽位

[template.example.json](template.example.json) 给出该变体的节点模板形状，预热体为
[warmup-request.json](warmup-request.json)（其 sha256 写在模板的
`readiness.warmup.body_sha256`）。它与
[VoiceDesign 模板](../file-voice-design/template.example.json) 必须声明同一个
`variant_slot`（`tts-variant`）：同节点只能常驻一个 TTS 变体，两个变体同时装载时第二个
实例在 stage1 以 `CUDA error: out of memory` 失败。控制器在 prepare 时拒绝同槽位仍有
未 drain 实例的请求（HTTP 409 `intent_conflict`），槽位只在旧实例取得已核验 stop 后释放。

注意：样例中的 `<CUSTOMVOICE_MODEL_ROOT>`、`<SERVED_MODEL>`、`<NATIVE_SPEAKER_ID>` 与
`readiness.warmup.body_path` 由部署方按实际挂载替换；`command` 与镜像摘要沿用
[runtime-pin.json](runtime-pin.json) 固定的只读核验记录，部署前仍须核对实际 argv 与环境。
