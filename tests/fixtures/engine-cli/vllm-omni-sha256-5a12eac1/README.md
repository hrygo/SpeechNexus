# vLLM-Omni CLI surface, captured from the pinned image (#1075)

`deploy/engines/*/template.example.json` 固定 `vllm/vllm-omni:latest`
`@sha256:5a12eac1a2dd3540a6ec29e42ef0dd5286c75ee07f68e497cb5f0df6e1311a52`。本目录记录该镜像
自报的 `serve` 参数面与两次实测调用，供 `tests/ci/test_engine_template_argv.py` 校验模板
argv，而不是让人凭记忆判断哪个参数可用。

## 记录来源

- 镜像：`vllm/vllm-omni@sha256:5a12eac1a2dd3540a6ec29e42ef0dd5286c75ee07f68e497cb5f0df6e1311a52`
  （`docker image inspect` 的 `Id` 与 `RepoDigests` 同为此摘要）。
- 镜像内版本：`vllm_omni 0.32.0rc1`、`vllm 0.32.0`（取自镜像内两个 `*.dist-info/METADATA`
  路径）。
- 采集时间：2026-11-20（Asia/Shanghai）。
- 采集主机：arm64 macOS Docker，**无 CUDA 设备**。

## 无 GPU 时如何采集

`vllm-omni --help` 在无 CUDA 主机上会在构造解析器时失败：vLLM 的 `DeviceConfig` 默认工厂
调用 `current_platform.device_type`，未指定平台时抛
`RuntimeError: Failed to infer device type`。为拿到解析器自报的参数面，采集脚本在导入 CLI
后只做一件事：把 `vllm.platforms.current_platform.device_type` 置为 `"cpu"`，再调用镜像
自带的 `vllm_omni.entrypoints.cli.main.main()`。**参数解析与命令分派仍是镜像自己的代码**，
唯一改动是设备类型，因此下面的 accept/reject 结论来自该镜像的 argparse 面，未经验证的只是
「真实 GPU 加载权重」这一段。

`--omni` 的语义是该入口的分支开关：`main()` 先看 `sys.argv` 里有没有精确的 `--omni`；没有
就转交上游 vLLM 的 `main()`，有才用 vLLM-Omni 自己的解析器与 `serve` 子命令
（`vllm_omni/entrypoints/cli/main.py`）。这解释了两个 help 面的差别，也是模板旧 argv 失败的
机制。

## 文件

| 文件 | 内容 |
| --- | --- |
| `serve-options-plain.txt` | 不含 `--omni` 时（上游 vLLM 解析器）`serve --help=all` 里以 `  --name` 开头的选项集合，已排序去重，316 项 |
| `serve-options-omni.txt` | 含 `--omni` 时（vLLM-Omni 解析器）同一命令的选项集合，已排序去重，410 项 |
| `rejected-task-type.stderr` | 模板旧 argv 的实测输出：`serve /nonexistent-model --task-type CustomVoice` |
| `accepted-omni.txt` | 修正后 argv 的实测输出：`serve /nonexistent-model --omni` |

两个 `--help=all` 原始转储（各约 2400/2800 行）未入库，只保留派生选项集合；原始转储的
SHA-256 为 `8dcca164727cb81a8dbf9d045a91d7f80fa0f48ed389f63ff87e73f18b5ffbec`
（plain）与 `8ae6e80f2f8a3722fabae7943d6cd52051b9deb0d9a9f22899c67bb32e3b8dbf`（omni）。
`accepted-omni.txt` 与 `rejected-task-type.stderr` 已去掉 ANSI 颜色、logo 与 traceback 的
中间帧，保留版本横幅、`--omni` 分支的 api_server 启动日志和结尾错误；`exit=` 一行由采集
脚本打印（它捕获 `SystemExit` 以便同时留下退出码）。

## 对模板的含义

- 模板旧 argv 只有 `--task-type <Kind>`、没有 `--omni`：入口转交上游 vLLM 解析器，而该解析器
  不认 `--task-type`，于是 `error: unrecognized arguments: --task-type CustomVoice`、退出码 2。
- 加上 `--omni` 后同一个参数是被识别的（`serve-options-omni.txt` 含 `--task-type`，其帮助为
  “Model-defined startup task type. The selected model validates supported values”），但模板
  不传它：任务由 checkpoint 决定，实测可用的启动形态（见
  `docs/archive/audits/2026-10-24-b03-tts-engine-feasibility.md`）与参考部署里正在运行的
  `vllm-omni serve <model> --omni` 都不带它，本仓库没有带 `--task-type` 的真实加载证据。