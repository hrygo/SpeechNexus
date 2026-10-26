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

详见[制品、参数与部署交接](../../../docs/architecture/file-tts-binding-handoff.md)。
`runtime-pin.json` 是本次镜像只读核验的固定记录，离线模型校验不重新证明运行容器或依赖。
