# 上线前检查脚本（C0–C12）

[`scripts/dev-env/release-preflight.sh`](../../scripts/dev-env/release-preflight.sh) 把
DEL-120 上线就绪评估中的 C0–C12 检查变成可执行门禁。脚本在仓库根目录运行，默认
只读；它不会写数据库、发送写请求、重启 `del109-*` 或触碰 `58030` 网关。

## 用法

```bash
# 默认只读，执行全部 C0-C12
./scripts/dev-env/release-preflight.sh

# 只跑指定项，便于定位（C5 默认 SKIP，C7 仍按断言）
./scripts/dev-env/release-preflight.sh --only C5,C7

# 候选冻结后把 HEAD 锁到批准的完整 SHA
./scripts/dev-env/release-preflight.sh --expect-head <完整 SHA>

# 仅在明确允许写库时执行公开 ASR/TTS E2E
./scripts/dev-env/release-preflight.sh --allow-write --only C5,C6
```

每项输出一行：

```text
[PASS] C2 网关就绪 | http=200 status=ready checks=2/2
[FAIL] C3 准入不变量 | unknown_quarantined=2 expected=0 ready_instances=1
[SKIP] C5 公开 ASR 真实 E2E | 需要 --allow-write（会写库，最长 130s）
```

最后输出 `SUMMARY`、失败/跳过 ID 和 `RESULT PASS|FAIL`。退出码：`0` 没有 FAIL，
`1` 至少一个 FAIL，`2` 参数错误。SKIP 不会把进程变成失败，但会在逐项输出和
`SKIPPED` 汇总中保留；它不代表该项已通过。

## 检查项与 SKIP 条件

| ID | 检查 | 何时 SKIP |
| --- | --- | --- |
| C0 | HEAD、`git status --porcelain`、Node/pnpm/uv/Python 版本；可用 `--expect-head` 锁定完整 SHA | 工具缺失；工作树不干净或 HEAD 不匹配时是 FAIL，不是 SKIP |
| C1 | 当前 HEAD 的 `gh run list`、run 状态和 job 数 | `gh`/`jq` 缺失、未认证、该 commit 尚无 run、run 尚未完成，或无法读取 job 列表 |
| C2 | `127.0.0.1:58030/readyz` 的 `status=ready` 与所有 check `ok` | `curl`/`jq` 缺失，或网关不可访问（例如 `58030` 未监听）；超时或断言失败是 FAIL |
| C3 | `unknown_quarantined` 必须为 `0`，ready 实例至少 `1` | `docker` 缺失、`del109-pg` 不存在/未运行，或只读查询不可用 |
| C4 | `complete_successfully(` 是否有生产调用点，而不是只有定义 | 仓库不可读或 `git grep` 执行失败；无匹配或只有定义是 FAIL |
| C5 | 公开 `/v1/audio/transcriptions` 真实 E2E | 默认 SKIP；没有 `--allow-write`，缺少配置或样本，`curl`/`jq` 不可用，或 API 未监听 |
| C6 | 公开 `/v1/audio/speech` 真实 E2E | 默认 SKIP；没有 `--allow-write`，缺少配置，`curl`/`jq`/`file` 不可用，或 API 未监听 |
| C7 | 资格报告必须是 `production`、`pass` 且所有 required 场景通过 | `jq` 缺失或报告文件不可读 |
| C8 | 候选与随仓 binding 定义各跑一次证据校验，二者都必须精确 `RESULT PASS` | `uv` 缺失、输入文件不可读，或 verifier 未产生任何 `RESULT`；`PASS_WITH_WARNINGS` 和 `FAIL` 都是 FAIL |
| C9 | `docker compose config --services` 的必需服务集合，以及 gateway 的 file-ASR/file-TTS 私有配置引用 | `docker` 或 compose 命令不可用；compose 无法解析或集合不完整是 FAIL |
| C10 | `speechnexus-publish-binding --help` 是否含 retire/activate/rollback | 命令不可执行且 `uv --no-sync` 也无法读取 help；help 可读但缺选项是 FAIL |
| C11 | `docs/operations` 中是否存在对账恢复入口 | `rg`/`grep` 不可用；没有匹配是 FAIL |
| C12 | 最终独立验收 | 始终 SKIP；由独立验收人/agent 在最终候选上判定，脚本不代替 |

## 环境、凭据与只读边界

- 脚本优先 source `SPEECHNEXUS_TOOLCHAIN_ENV`；未设置时使用部署映射中的
  `native-20261027-v1/environment.sh`。C0 仍会实际执行版本命令并核对版本。
- C5/C6 从既有 env 文件读取配置。默认尝试
  `.paperclip-runtime/del109/api.env`、`.paperclip-runtime/del109/api-transcription.env`、
  `.runtime/config/api.env`、`.runtime/config/release-preflight.env`；设置
  `SN_PREFLIGHT_ENV` 时只读该文件。脚本不内联凭据，也不回显密钥或业务正文。
- C5/C6 是唯一会写库的检查，且只有显式 `--allow-write` 才执行。C12 永远 SKIP。
- C8 会用 `uv --no-project --with` 的临时环境执行校验器；它只使用 workspace 本地 uv
  缓存，不安装项目依赖、不写产品或服务运行态。
- C9 只解析 `deploy/compose.yaml`；它不启动、不重启任何服务，也不验证容器实际运行。

## 证据边界

C0–C11 只证明脚本实际读取到的局部事实。C1 的分支 CI 不替代部署后验证；C4 的静态
调用点检查不证明真实成功执行；C7/C8 的格式与摘要校验不替代 live 生产资格；C9 的
compose 集合不替代真实多机拓扑；C10 的 help 选项不证明 CAS/ETag/回滚行为；C11 的
文档匹配不证明恢复演练。C12 必须由独立验收人/agent 在冻结候选上完成，并区分真实
引擎、隔离存储、设备与物理拓扑。脚本在任何拿不到证据的地方输出 SKIP，而不是 PASS。
