# AFXX 配置

本文说明配置选择、字段默认值、环境初始化和恢复约束。日常命令流程见 [README](../README.md)，完整示例见 [带注释的 YAML 模板](../afxx/resources/config.example.yaml)。

## 创建和选择配置

```bash
afxx config init --source /absolute/path/graph-autofusion --output ~/.config/afxx/config.yaml
```

`config init` 自动填写 `local.source_dir`，保留模板注释并拒绝覆盖已有文件。填写 `connection.environment`、`workflow_root`、`build.llvm_build_dir` 和 `build.lld_bin`；根据需要修改 Python/CANN 选项。私人配置不要纳入版本控制。

所有命令（包括 env init/update）默认读取 `~/.config/afxx/config.yaml`，仅显式 `--config` 覆盖。不读取 AFXX_CONFIG、XDG_CONFIG_HOME 或工作目录配置。`config init --source <源码目录>` 在默认位置生成配置；`--output` 可指定其他位置，已有文件不会覆盖。

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
├── workflow-results/<job-id>/    本次 AFXX replay 证据目录
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
| `upload.enabled` | false；fuzz replay 仅保存本地结果，不接受上传覆盖 |

Python 3.11 由工具自动准备；LLVM 和 LLD 仍由远端提供。修改 workflow_root 只影响路径推导；创建新环境应使用新目录，已有 venv 和编译缓存包含绝对路径。

## 初始化和更新环境

```bash
afxx env init --config ~/.config/afxx/config.yaml
afxx env check
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
| `index_url` | 普通 Python 包索引，null 使用 uv 默认索引 |
| `torch_index_url` | torch 索引覆盖；带 +cpu 的版本默认使用 PyTorch CPU 索引 |
| `torch_npu.url_template` | Daily 下载源覆盖，默认来自 package_sources.json |
| `torch_npu.sha256` | 可选预期哈希，校验 Daily 压缩包 |
| `stable_requirements` | 完整稳定依赖覆盖，与 torch_version 二选一 |

`env init` 使用上述版本配置创建环境。`env update` 只使用配置定位目标环境和包源，以已安装环境清单为基础应用显式更新参数，不把配置中的旧版本重新装回去，默认不改写用户配置。

```bash
# 只查询当前版本，不更新任何包
afxx env update
# 更新一个或多个选定组件
afxx env update --afx latest
# 更新成功后，将实际版本写回本次使用的配置文件
afxx env update --afx latest --save-config
afxx env update --torch-npu 20261123
afxx env update --torch 2.10.0+cpu --torch-npu latest
# Toolkit；或 Toolkit + 同批次 OPS
afxx env update --cann latest
afxx env update --cann 20261123 --ops
```

`--torch` 使用精确版本；变更 torch 必须同时指定 `--torch-npu`，Daily 按目标 torch 版本匹配。只更新 torch_npu 时匹配实际已安装 torch。torch 联动更新允许解析所需依赖；其他 Python 更新保留非目标包的版本和来源，依赖冲突在安装前报错。选定包哈希未变化时跳过。无参数查询仍返回 job，可从日志和结果中的 installed 查看当前包版本及 CANN 清单。

CANN 更新只接受日期或 latest，不允许降级；`--ops` 必须同时提供 `--cann`。OPS 型号使用配置覆盖或自动探测，先确定 Toolkit 发布目录再查对应 OPS，缺包在安装前失败，不回退到其他批次。未指定 OPS 时保留现有 OPS 并记录其原批次，已知跨越不兼容主版本则要求一起更新。NNAL 不在本次更新范围内。Toolkit/OPS 使用官方升级接口，所有目标 run 包先下载并校验，成功后清理安装包，失败保留现场。跨版本组件兼容性仍需实际环境验证。

Python 更新失败时尝试旧锁回滚；一旦开始 CANN 安装，不承诺自动回滚，失败标记环境 invalid 并保留报告与安装日志。成功后保存实际版本、来源和哈希。更新要求 ready 的受管理环境，已有包清单、锁和缓存仍需有效；平台自动升级或手工修改造成清单漂移时，需先核对处理。不会同步 token，也不会自动停止或重启客户端，仍检查 venv 占用，更新 CANN 时额外检查其安装目录占用。

包源默认值及 OPS 映射集中在 [package_sources.json](../afxx/environment/package_sources.json)。

## CANN 准备

init 在创建 venv 前检查安装根目录。存在 `cann/set_env.sh` 和 `cann/compiler/version.info` 时复用安装；根目录不存在或为空时按配置下载安装；目录非空但标志不完整时直接报错。

| CANN 选项 | 行为 |
|---|---|
| `date` | latest 选择最新完整发布；YYYYMMDD 选择当天最新完整发布 |
| `install_root` | 安装根目录覆盖 |
| `ops` | null/auto 根据 npu-smi 完整型号查映射；指定系列直接选包；none 跳过 OPS |
| `ops_compatibility` | 扩展 package_sources.json 中的型号映射；未知型号需要核实后补充 |
| `nnal` | 默认 false，需要 ATB/SiP 时启用 |
| `driver_library_dirs` | 可选覆盖；省略或空列表时，从 npu-smi 实际位置、/etc/ascend_install.info 和 /usr/local/Ascend/driver 自动探测已有驱动库，保留继承的 LD_LIBRARY_PATH，仅用于 npu-smi 子进程，不安装驱动 |
| `repository_url` | CANN 包源覆盖 |
| `auto_install` | 默认 true，允许空目录安装 |

Toolkit、所需 OPS 和可选 NNAL 必须属于同一完整发布。下载到 `/tmp/af-cann-<job>-<随机后缀>/`，执行包校验并记录 SHA-256，安装成功后清理下载包。失败保留现场；临时目录受系统清理策略影响。驱动和固件由机器环境提供。

## 平台连接

模板默认启用 platform 节，确认 HTTPS 地址和 agent_id；token 文件路径通常保持 null 使用默认值。两端使用同一平台用户的 Bearer token。凭据文件放在源码目录外，远端文件权限需为 0600。

```yaml
platform:
  url: "https://autofuse.online"
  agent_id: "af-repair-agent"
  token_file: "/absolute/local/path/agent.token"
  remote_token_file: "/absolute/remote/path/agent.token"
  client_token_file: "/absolute/remote/path/client.token"
```

AFXX 的 token 需要 `issue` 权限。远端独立运行的 AFX 客户端使用自己的 client token，需要 `upload/retest` 权限；不要将默认只具备客户端权限的 token 用于 AFXX 领取和提交问题。

`platform.client_token_file` 为远端已有 token 文件路径，首次初始化 client 时必填；已有 managed client 可以复用其保存的凭据。AFX >= 0.4.1 公开 init/start/status 管理客户端，设备沿用 replay.devices。客户端目录由远端 AFX 自身解析（AUTOFUSEX_HOME、active-home 指针或系统默认目录）。启动读取平台配置，可能同时执行 fuzz；已有 client 的 endpoint、token 和设备配置由 AFX 管理。AFXX 不自动停止或重启驻留进程。详见 README 的 AFX 常驻客户端章节。

## Job 环境快照

deploy/replay/check 在远端开始执行时保留提交的 `config.yaml`，并现场采集 `environment.json`：Python 版本和解释器路径、已安装 Python 包版本及安装来源（包含安装元数据已有的包哈希）、CANN 路径和版本、实际加载的 AF 库路径和哈希。采集失败的项目写入 errors，不使用旧 init/update 清单补作当前值。

日常任务不再比对配置与 init/update 清单，也不要求缓存 wheel 或旧锁文件存在。必需依赖无法加载仍会报错；replay 不再引用部署记录。环境快照用于追溯，不能单凭版本列表保证精确重建；未发布 wheel 的原始来源可能需要另外保存。

日常 deploy 检查构建与安装验证所需依赖；replay 检查用例执行及 seed/results 接口。全量依赖一致性检查（pip check）在 init/update 和 env check 中执行。replay 的 environment.json/submitter_runtime 是启动端记录，执行环境来自 attempt.contexts.environment 并保存于 execution_environments。

安装 AF 前只检查本次包安装清单中的共享库是否被可见进程加载，不阻止仅使用其他 CANN 库的进程。init/update 共用入口在目标 venv 已存在时仍检查整个 venv 的占用。

## 从冻结配置重建环境

将成功 init/update 任务的 config.yaml 取回本地，执行 `init --config /absolute/path/config.yaml`。目标 venv 要求尚不存在。异地重建时调整运行、状态和源码路径，保留 environment_lock 和包来源。

恢复使用内嵌 Python 依赖锁、实际 Daily 日期、CANN 发布号及包哈希。基础 Python 版本必须匹配，包源或共享 wheel 需仍可访问。外部 CANN 缺少安装收据时，只记录可探测的版本信息，恢复需准备相应安装。源码、seed 和运行结果按各自产物保存。

MCP 默认读取同一固定配置；需要覆盖时，通过调用的 `config` 字段、`arguments` 中的 `--config`，或启动 `afxx mcp --config ...` 提供。

## Fuzz replay 与定位证据

AFXX 使用 AFX 0.4.1+ 的 `afx fuzz replay <seed> --save`，不强制指定数据库。有存活 client 时复用其 home 和队列，无 client 时由 AFX 独立执行；无需本机 seed 历史。默认开启 upload，只上传本轮明确关联的 attempt；`--no-upload` 可关闭，但正在自动上传的共享 session 会被明确拒绝，需先停止该 session。

AFXX request_id/run 保存队列路径、task ID、attempt ID、结果摘要和日志，不复制共享数据库。结果通过 `afx results list <database> --attempt <id>` 精确读取；task→attempt 尚无公开查询接口，仅在适配层只读查询队列表。队列任务由 client 上传，AFXX 标记 upload.owner=client、state=delegated，不再执行整批补传；delegated 不等于服务端已确认。仅独立执行的 attempt 由 AFXX 通过临时数据库调用 `afx sync once`，结束即清理。

`--dump` 或 `replay.dump: true` 传递给 AFX 逐任务开启采集。使用 `evidence list/read --request-id ID --run N` 按需读取远端产物；不自动复制整个共享 dump 目录。新增或发生文件变化的 worker 目录仅标为候选证据，只有诊断中的明确路径才能关联到 attempt；文件种类及保留期限由 AFX 管理，缺少 IR/kernel 时仍需报告证据缺口。

seed 顺序提交；devices=auto 交给 AFX 调度，显式设备轮转传入。compile_threads 传给 AFX；batch_timeout_seconds 限制单 seed 提交及等待时间，超时保留回执，不自动重提。workers 不控制该入口的并发。


## 平台连接

新配置默认启用 platform。AFXX 当前不支持无 token 的游客平台初始化：交互 init 会要求输入 token，非交互调用缺少 token 文件时报错，均不会匿名跳过身份校验。只需环境初始化和部署时，可以删除或注释整个 platform 节；此时 init 不读取或创建 token，平台 issue 操作仍需配置凭据。配置 platform.url 和 agent_id 后：

- platform.token_file 省略、null 或空字符串时，默认 `~/.config/afxx/auth/afx.token`。
- platform.remote_token_file 留空时，默认 `<workflow_root>/auth/afx.token`。
- platform.client_token_file 留空时，复用 remote_token_file；显式指定时两处都会收到同一个 token。

init 提交前复用本地 token；文件不存在时仅在交互终端隐藏输入一次，保存为 0600，再通过 `/api/me` 验证身份。已存在但为空或不可读的文件会报错，不静默覆盖。本地 token 必须位于同步源码目录之外。token 由平台签发，AFXX 不签发、不轮换；用户需具有相应领取、验证、上传和复测权限，具体权限仍由平台接口检查。

环境 init 成功后通过 SSH 标准输入同步到远端，文件权限为 0600，token 内容不进入命令参数、配置快照、job 文件或日志。相同 token 复用；任一目标已有不同 token 时停止，不覆盖原凭据。配置文件只保存路径。

普通 env init 在返回前完成同步。`--detach`、MCP 或中途停止查看日志的任务，需要后续 `status` / `status --wait` 确认成功并同步；仅提交任务不表示 token 已就绪。MCP/非交互调用不会弹出输入，缺少本地文件返回 PLATFORM_CREDENTIAL_MISSING。dry-run 不读取、不输入、不同步 token；update 不触发凭据同步。

成功结果的 auth.state 为 ready。凭据同步失败时返回 PLATFORM_CREDENTIAL_SYNC_FAILED 或 PLATFORM_CREDENTIAL_CONFLICT，并保留 job_id、environment_state=success；修正文件/权限后查询同一 init job 重试，不重新初始化 venv。续查使用本地 job 的冻结配置定位凭据文件，重复同步不会重写相同内容。不要把另一环境的配置用于恢复本任务。

### Fuzz 目录与资源

`afxx fuzz run` 复用 connection、runtime.venv_dir 和 runtime.install_root，不需要新增配置节。AFX home 固定为 `<workflow_root>/fuzz/`，结果为其下 `results/results.sqlite`，队列为 `tasks/tasks.sqlite`。`replay.results_dir` 和 `replay` 的资源参数仅影响 replay；fuzz 默认由 AFX 自动发现可见 NPU 并选择资源预算，单次覆盖通过 fuzz run 的命令行参数指定。控制现有 fuzz job 时使用该 job 冻结的环境配置，并核对共享目录的归属。已有运行时新任务失败；旧 job ID 不能控制接管后的新运行。

### Replay 一次性上传

`upload.enabled: true` 或 replay 的 `--upload` 使用 `afx platform client once`，读取本次 `results.sqlite`，将上传游标保存在同目录的 `platform-sync.sqlite`。凭据使用远端 `platform.client_token_file`，未指定时回退到 `platform.remote_token_file`；本地 `platform.token_file` 不作为远端凭据。token 需具备上传和心跳权限。`--no-upload` 覆盖默认配置。

移除配置中旧的 `upload.sync_config_path`，不再使用 sync.json。此操作不启动常驻进程、不执行复测或自动更新。上传命令失败会使 replay job 失败，完整复现证据仍保留。`upload=completed` 仅表示官方命令及回执检查完成，回执 uploaded 计数不保证所有记录被平台接收。

AFX 仅接受 `python_env.afx.version`（默认 `latest`），不再接受 wheel 路径、URL 或用户配置的 sha256。下载包的实际哈希仍自动记录在环境清单与 environment_lock 中，用于缓存校验和冻结恢复；冻结配置将 latest 展开为实际版本。

`env update --save-config` 仅在更新成功后回写选中组件的实际版本/日期（`latest` 展开为具体值），保留注释和其他字段。更新 torch_npu 时也记录匹配的 torch 版本。dry-run 不写文件；`--detach` 或 MCP 提交后，由后续 `status` / 等待终态完成本地回写。回写目标是提交时使用的配置文件；等待期间文件被编辑则报冲突，不覆盖编辑，远端成功的更新不因此回滚。重复查询复用回写结果。

单个 `cann.date` 无法表达不同批次的 Toolkit 和 OPS。若选用 `--save-config` 会产生这种组合，则安装前拒绝；使用 `--cann DATE --ops --save-config` 同批更新，或省略回写参数。

AFX 公共 CLI 暂未暴露完整 AF 加载身份和 NNSmith 源码校验；这些探测仍在适配层调用新版 execution/workloads 模块。client 操作与结果查询使用公开 CLI。
