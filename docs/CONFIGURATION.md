# AF Forge 配置

本文说明配置选择、字段默认值、环境初始化和恢复约束。日常命令流程见 [README](../README.md)，完整示例见 [带注释的 YAML 模板](../af_forge/resources/config.example.yaml)。

## 创建和选择配置

```bash
af-forge config init --source /absolute/path/graph-autofusion --output ~/.config/af-forge/config.yaml
```

`config init` 自动填写 `local.source_dir`，保留模板注释并拒绝覆盖已有文件。填写 `connection.environment`、`workflow_root`、`build.llvm_build_dir` 和 `build.lld_bin`；根据需要修改 Python/CANN 选项。私人配置不要纳入版本控制。

所有命令（包括 env init/update）默认读取 `~/.config/af-forge/config.yaml`，仅显式 `--config` 覆盖。不读取 AF_FORGE_CONFIG、XDG_CONFIG_HOME 或工作目录配置。`config init --source <源码目录>` 在默认位置生成配置；`--output` 可指定其他位置，已有文件不会覆盖。

配置使用 YAML 普通映射、列表和标量。日期、版本号使用带引号的字符串。加载器拒绝重复键、自定义标签、合并键及不支持的数据类型。路径填写绝对路径，配置中的 `~`、`$HOME` 不展开。远端运行路径采用字母、数字及 `_./+-`，避免空格和 shell 元字符。

## 目录与默认值

`workflow_root` 推导远端目录，显式配置的路径覆盖对应默认值。源码同步目标应使用专用目录，首次同步要求目录为空。

```text
<workflow_root>/
├── python/                       托管 Python 3.11、引导 uv 及下载缓存
├── venv/                         Python 3.11 环境
├── Ascend/cann/                  CANN home
├── graph-autofusion/
│   └── build/                    编译缓存
├── workflow-state/<job-id>/      任务配置、日志、报告及 AF run 包
├── workflow-results/<job-id>/    独立 replay 结果
└── workflow-env-state/           当前环境清单、锁文件和共享 wheel
```

| 配置项 | 默认行为 |
|---|---|
| `local.source_dir` | 必填，本地 graph-autofusion 源码仓库的绝对路径 |
| `local.state_dir` | 本机 `~/.local/state/graph-autofusion/dev_workflow/jobs` |
| `runtime.venv_dir` | `<workflow_root>/venv` |
| `cann.install_root` | `<workflow_root>/Ascend`，CANN home 为其下的 cann/ |
| `remote.source_dir` | `<workflow_root>/graph-autofusion` |
| `remote.state_dir` | `<workflow_root>/workflow-state` |
| `python_env.state_dir` | `<workflow_root>/workflow-env-state` |
| `replay.results_dir` | `<workflow_root>/workflow-results` |
| `build.jobs` | 8 |
| `replay.devices` / `workers` | auto，查询可见逻辑 NPU，每卡一个 worker |
| `replay.compile_threads` / `batch_timeout_seconds` / `dump` | 4 / 300 / false |
| `sync.include_ignored_paths` / `exclude_paths` | 空列表，相对于源码根目录的精确路径 |
| `upload.enabled` / `sync_config_path` | false / `<workflow_root>/sync.json` |

Python 3.11 由工具自动准备；LLVM 和 LLD 仍由远端提供。修改 workflow_root 只影响路径推导；创建新环境应使用新目录，已有 venv 和编译缓存包含绝对路径。

## 初始化和更新环境

```bash
af-forge env init --config ~/.config/af-forge/config.yaml
af-forge env check
```

init 自动上传配置和工具，在 `<workflow_root>/python/` 内准备独立 Python 3.11，无需预装系统 Python 3.11。引导阶段使用 curl 从 PyPI 下载固定版本 uv wheel，校验 SHA-256 后只取其中的 uv 可执行文件，再由 uv 下载托管 Python。下载过程不依赖系统 Python 的 SSL 或 SQLite。

在安装 CANN 和创建目标 venv 前，逐项检查 Python 版本、ssl、ctypes、venv、ensurepip、SQLite >=3.35，并实际创建临时 venv 验证 pip。失败停止初始化，任务状态为 `python_bootstrap` / `PYTHON_BOOTSTRAP_FAILED`，详情写入 `result.json` 的 `base_python` 字段。检查通过后准备 CANN，创建专用 venv，安装 uv、torch、torch_npu、AFX execution 依赖和 AF Python 依赖。

Python 环境锁包含 `nnsmith-autofusex==0.1.2.post1`，venv 内使用 uv 0.11.32。默认复用已经准备好的托管 Python，不在 update 时自动升级解释器；已有 venv 继续使用其原基础解释器。冻结配置保留实际 Python 补丁版本，恢复托管环境时按该版本下载。`python_env.base_python` 和 `uv_bin` 仅作为显式外部工具覆盖项，不需要日常填写。托管下载需要远端能够访问 PyPI 和 uv 的 Python 分发源。

普通 init 要求目标 venv 尚不存在，且环境状态目录没有其他环境清单。失败会保留现场和 invalid 标记。init/update 默认实时跟随远端日志，结束后输出 JSON 摘要。`--detach` 仅提交任务，后续用 status 查询。

| Python 选项 | 含义 |
|---|---|
| `torch_version` | 精确版本，默认 `2.10.0+cpu`；torch_npu 系列由此推导 |
| `torch_npu.date` | latest 或 YYYYMMDD；latest 从 UTC 当天回查最多 31 天 |
| `afx.version` | 默认 `latest`，从 PyPI 获取最新兼容正式版；可指定精确发布版本 |
| `afx.wheel` | 未发布 wheel 的远端绝对路径或 HTTPS URL，与 version 二选一 |
| `index_url` | 普通 Python 包索引，null 使用 uv 默认索引 |
| `torch_index_url` | torch 索引覆盖；带 +cpu 的版本默认使用 PyTorch CPU 索引 |
| `torch_npu.url_template` | Daily 下载源覆盖，默认来自 package_sources.json |
| `torch_npu.sha256` / `afx.sha256` | 可选预期哈希，分别校验 Daily 压缩包和 AFX wheel |
| `stable_requirements` | 完整稳定依赖覆盖，与 torch_version 二选一 |

修改 Daily 日期或 AFX 版本后执行 update。AFX 配置为 `latest` 时，每次 update 查询并下载当前最新兼容正式版，包哈希未变化则跳过更新；环境清单记录实际版本和哈希，冻结配置使用实际版本。它比较配置和当前环境清单，更新发生变化的 torch_npu/AFX，校验其余依赖的版本和来源。更新失败时尝试用原锁恢复，恢复失败则标记 invalid。torch 和稳定依赖变化需要在新目录 init。

更新环境：

```bash
af-forge env update --config ~/.config/af-forge/config.yaml
```

AFX 客户端由官方命令独立运行；Forge 在 verify 前负责启动检查，update 不停止或重启它。更新与安装保留环境占用检查，发现正在使用的库时会报错。

update 保留现有 CANN。包下载要求来源仍可访问；指定版本的包必须支持当前 Python 和平台。包源默认值及 OPS 映射集中在 [package_sources.json](../af_forge/environment/package_sources.json)。

## CANN 准备

init 在创建 venv 前检查安装根目录。存在 `cann/set_env.sh` 和 `cann/compiler/version.info` 时复用安装；根目录不存在或为空时按配置下载安装；目录非空但标志不完整时直接报错。

| CANN 选项 | 行为 |
|---|---|
| `date` | latest 选择最新完整发布；YYYYMMDD 选择当天最新完整发布 |
| `install_root` | 安装根目录覆盖 |
| `ops` | null/auto 根据 npu-smi 完整型号查映射；指定系列直接选包；none 跳过 OPS |
| `ops_compatibility` | 扩展 package_sources.json 中的型号映射；未知型号需要核实后补充 |
| `nnal` | 默认 false，需要 ATB/SiP 时启用 |
| `driver_library_dirs` | npu-smi 所需驱动库目录 |
| `repository_url` | CANN 包源覆盖 |
| `auto_install` | 默认 true，允许空目录安装 |

Toolkit、所需 OPS 和可选 NNAL 必须属于同一完整发布。下载到 `/tmp/af-cann-<job>-<随机后缀>/`，执行包校验并记录 SHA-256，安装成功后清理下载包。失败保留现场；临时目录受系统清理策略影响。驱动和固件由机器环境提供。

## 平台连接

启用模板中的 platform 节，填写 HTTPS 地址、agent_id、本地及远端 token 文件路径。两端使用同一平台用户的 Bearer token。凭据文件放在源码目录外，远端文件权限需为 0600。

```yaml
platform:
  url: "https://autofuse.online"
  agent_id: "af-repair-agent"
  token_file: "/absolute/local/path/agent.token"
  remote_token_file: "/absolute/remote/path/agent.token"
  client_token_file: "/absolute/remote/path/client.token"
```

Forge 的 token 需要 `issue` 权限。远端独立运行的 AFX 客户端使用自己的 client token，需要 `upload/retest` 权限；不要将默认只具备客户端权限的 token 用于 Forge 领取和提交问题。

`platform.client_token_file` 在 issue verify 时必填，为远端路径。Forge 先用官方 `client once` 检查连通和鉴权，再后台运行 `client run --retest`，设备沿用 replay.devices（auto 时查询可见卡）。启动证据和日志位于 remote.state_dir 的上一级目录下的 afx-client/。相同配置和 AF 身份复用已有进程；不自动停止旧进程。启动检查不等同于复测通过，结果仍通过 issue status/wait 查询。

## Job 环境快照

deploy/replay/check 在远端开始执行时保留提交的 `config.yaml`，并现场采集 `environment.json`：Python 版本和解释器路径、已安装 Python 包版本及安装来源（包含安装元数据已有的包哈希）、CANN 路径和版本、实际加载的 AF 库路径和哈希。采集失败的项目写入 errors，不使用旧 init/update 清单补作当前值。

日常任务不再比对配置与 init/update 清单，也不要求缓存 wheel 或旧锁文件存在。必需依赖无法加载、replay 指定的 AF 部署被替换仍会报错。环境快照用于追溯，不能单凭版本列表保证精确重建；未发布 wheel 的原始来源可能需要另外保存。

日常 deploy 检查构建与安装验证所需依赖；replay 检查用例执行及 seed/results 接口。全量依赖一致性检查（pip check）在 init/update 和 env check 中执行。replay 在本次任务持锁期间复用环境快照的 AF 探测结果。

安装 AF 前只检查本次包安装清单中的共享库是否被可见进程加载，不阻止仅使用其他 CANN 库的进程。init/update 共用入口在目标 venv 已存在时仍检查整个 venv 的占用。

## 从冻结配置重建环境

将成功 init/update 任务的 config.yaml 取回本地，执行 `init --config /absolute/path/config.yaml`。目标 venv 要求尚不存在。异地重建时调整运行、状态和源码路径，保留 environment_lock 和包来源。

恢复使用内嵌 Python 依赖锁、实际 Daily 日期、CANN 发布号及包哈希。基础 Python 版本必须匹配，包源或共享 wheel 需仍可访问。外部 CANN 缺少安装收据时，只记录可探测的版本信息，恢复需准备相应安装。源码、seed 和运行结果按各自产物保存。

MCP 默认读取同一固定配置；需要覆盖时，通过调用的 `config` 字段、`arguments` 中的 `--config`，或启动 `af-forge-mcp --config ...` 提供。

## Replay 定位采集

普通 replay 默认 `replay.dump: false`。修复 skill 使用 `replay --dump` 为本次任务开启以下选项，无需修改配置或更新 Python 环境：

```yaml
replay:
  dump: true
```

开启后，Forge 设置 AF `codegen_compile_debug=true`（debug_dir 为结果目录下的 dump/）、`TORCH_COMPILE_DEBUG=1`、`DUMP_GE_GRAPH=1`、`DUMP_GRAPH_LEVEL=1`、`DUMP_GRAPH_PATH=<结果目录>/dump/ge`，以及 `ASCEND_GLOBAL_LOG_LEVEL=0`、`ASCEND_SLOG_PRINT_TO_STDOUT=1`。实际注入参数记录于 job 的 result.json/replay_environment；dump 参数仅传给实际执行用例的 AFX 进程及其子进程。设备自动探测、seed 解析、结果查询和上传沿用基础环境及本次任务的 AUTOFUSEX_HOME。

AF 图、逐 pass MLIR 和生成代码位于结果目录 dump/；GE 图位于 dump/ge/；PyTorch 调试产物默认位于 job 目录 torch_compile_debug/，Inductor cache 位于结果目录 cache/。日志保留在 job.log 和 AFX worker 日志中。只有执行到对应阶段才产生文件，具体产物取决于后端及安装版本。

`--save` 始终保存 AFX 结果到 results.sqlite，与 dump 独立。dump 不自动保存所有输入/输出张量，也不强制同步运行或禁用缓存。性能验收应关闭 dump，避免调试开销影响结论。完整收集标准见 [复现证据清单](../skills/af-forge-repair/references/replay-evidence.md)。

replay 到达 success/failed 后，本地 `status` 或 `status --wait` 自动将完整 dump、日志、报告、结果数据库和清单下载到 `/tmp/af-forge-evidence-<uid>/<job-id>-<环境标识>/`。返回仅包含 job_id、state、evidence_path 和 failures（attempt_id、seed、backend、shape_indices、stage、简短 error）；失败超过 20 项时附数量和截断标记。异步提交后仍需调用 status/wait，远端不会主动推送文件。重复查询校验并复用本地文件，不重复下载。 下载覆盖结果目录和 job 的诊断目录，不受报告摘录的 128 文件/文本长度限制；不跟随符号链接，不下载含凭据的 sync.json，跳过原因记在 evidence.json。开启 dump 不保证每个阶段都生成文件。


## Init 平台凭据准备

未配置 platform 时，init 不读取或创建 token。配置 platform.url 和 agent_id 后：

- platform.token_file 省略、null 或空字符串时，默认 `~/.config/af-forge/auth/afx.token`。
- platform.remote_token_file 留空时，默认 `<workflow_root>/auth/afx.token`。
- platform.client_token_file 留空时，复用 remote_token_file；显式指定时两处都会收到同一个 token。

init 提交前复用本地 token；文件不存在时仅在交互终端隐藏输入一次，保存为 0600，再通过 `/api/me` 验证身份。已存在但为空或不可读的文件会报错，不静默覆盖。本地 token 必须位于同步源码目录之外。token 由平台签发，Forge 不签发、不轮换；用户需具有相应领取、验证、上传和复测权限，具体权限仍由平台接口检查。

环境 init 成功后通过 SSH 标准输入同步到远端，文件权限为 0600，token 内容不进入命令参数、配置快照、job 文件或日志。相同 token 复用；任一目标已有不同 token 时停止，不覆盖原凭据。配置文件只保存路径。

普通 env init 在返回前完成同步。`--detach`、MCP 或中途停止查看日志的任务，需要后续 `status` / `status --wait` 确认成功并同步；仅提交任务不表示 token 已就绪。MCP/非交互调用不会弹出输入，缺少本地文件返回 PLATFORM_CREDENTIAL_MISSING。dry-run 不读取、不输入、不同步 token；update 不触发凭据同步。

成功结果的 auth.state 为 ready。凭据同步失败时返回 PLATFORM_CREDENTIAL_SYNC_FAILED 或 PLATFORM_CREDENTIAL_CONFLICT，并保留 job_id、environment_state=success；修正文件/权限后查询同一 init job 重试，不重新初始化 venv。续查使用本地 job 的冻结配置定位凭据文件，重复同步不会重写相同内容。不要把另一环境的配置用于恢复本任务。
