# AF Forge

本工具从本地工作区同步源码到 NPU 环境，构建和安装 AF，并管理 Python 环境、固定 seed 复跑及 AFX 平台验证。源码同步包含未提交修改，使用 npursync 传输冻结快照。

安装后使用 `af-forge` 命令；`af-forge-mcp` 提供 stdio MCP 接口。命令和远端任务共用同一套业务模块，上传文件从已安装的 wheel 中取得，无需保留本工具源码。配置按 `--config`、`AF_FORGE_CONFIG`、当前目录 `af-forge-config.yaml`、用户配置目录 `~/.config/af-forge/config.yaml` 的顺序选择（支持 `XDG_CONFIG_HOME`）。源码仓库由 `local.source_dir` 指定，命令可在任意目录执行。

- [配置模板](af_forge/resources/config.example.yaml)
- [本地依赖](docs/LOCAL_DEPENDENCIES.md)
- [Agent 调用约定](docs/AGENT_USAGE.md)
- [设计与模块边界](docs/DESIGN.md)

## 工具目录

```text
af-forge/
├── README.md
├── pyproject.toml          # wheel、依赖和命令入口
├── af_forge/               # Python 包
│   ├── cli.py              # CLI 命令入口
│   ├── mcp_server.py       # stdio MCP 到 CLI 协议适配
│   ├── resources/config.example.yaml # 带注释配置模板
│   ├── common.py           # 文件、锁、进程等公共工具
│   ├── config.py           # 配置加载与校验
│   ├── bundle.py           # 远端运行文件清单
│   ├── source.py           # 源码同步
│   ├── jobs.py             # 远端作业协议
│   ├── execution.py        # deploy/replay
│   ├── environment/        # 环境管理
│   │   ├── runtime.py
│   │   ├── consistency.py
│   │   ├── python.py
│   │   ├── cann.py
│   │   └── package_sources.json
│   └── afx/                # AFX 接口适配
│       ├── replay.py
│       └── platform.py
├── docs/                   # Agent 规范、设计、本地依赖
└── tests/                  # 单元与接口测试
```

内部模块通过 `af_forge` 包导入；远端任务保留相同包结构。日常调用已安装的 `af-forge`；agent 也可通过 `af-forge-mcp` 使用同一组命令。

查看全部命令：`af-forge help`（等同于 `af-forge --help`，无需配置或连接远端）。

## 准备配置与依赖

```bash
# 使用 Python 3.11+ 从 wheel 安装。
python3 -m pip install /absolute/path/af_forge-0.1.0-py3-none-any.whl
npm install -g npussh@0.2.4
npussh login
af-forge config init --source /absolute/path/graph-autofusion --output ~/.config/af-forge/config.yaml
```

默认使用 pip，本地无需安装 uv。PyYAML 和 MCP SDK 随 wheel 依赖自动安装；Git、Node.js 20+、npussh/npursync、OpenSSH 和 rsync 仍由系统提供。确保安装工具的 bin 目录在 PATH 中。

如果希望将工具及其依赖隔离安装，也可以选择 `uv tool`：

```bash
uv tool install --python 3.11 /absolute/path/af_forge-0.1.0-py3-none-any.whl
```

远端 `init/update` 使用 uv 管理运行环境，与本地选择 pip 或 uv 安装工具无关。

编辑生成的配置，填写连接环境、workflow_root、远端 LLVM/LLD 路径及 Python/CANN 选项。`config init` 保留字段注释，自动填写 `local.source_dir`，拒绝覆盖已有文件。私人配置不要纳入版本控制。模板和工具源码均无需放进 graph-autofusion 仓库。

源码目录由配置显式指定，工具目录可以放在仓库之外；部署仍遵循 graph-autofusion 的 build.sh、构建产物和安装约定。

配置使用 YAML 普通映射、列表和标量。日期、版本号使用带引号的字符串。加载器拒绝重复键、自定义标签、合并键及不支持的数据类型。路径填写绝对路径，配置中的 `~`、`$HOME` 不展开。远端运行路径采用字母、数字及 `_./+-`，避免空格和 shell 元字符。

```bash
af-forge doctor
```

doctor 校验配置、本地连接工具和 npussh 登录。远端依赖由 init/check 及各操作的预检负责。

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
af-forge init
af-forge check
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
af-forge update
```

AFX 客户端独立运行，Forge 不控制其生命周期。更新与安装保留环境占用检查，发现正在使用的库时会报错。

update 保留现有 CANN。包下载要求来源仍可访问；指定版本的包必须支持当前 Python 和平台。包源默认值及 OPS 映射集中在 [package_sources.json](af_forge/environment/package_sources.json)。

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

## 部署源码与 AF

```bash
af-forge deploy --request-id deploy-001
af-forge status --job-id <job-id>
```

提交返回 job ID。远端先核对配置、当前环境清单、实际 Python 包和 CANN，再同步源码，执行 `bash build.sh --pkg --enable-mlir -j 8`，将本轮产物写入 job 的 packages/，随后校验并升级安装 AF。安装后验证实际加载的 pyautofuse、adapter、codegen 文件与本轮构建产物一致。

deploy/replay 的配置、当前环境清单与实际包版本不一致时，输出含 expected/actual 的 WARNING 并继续；告警保留在 job.log 中。有差异时保留提交的 config.yaml，不写入旧清单的环境锁，因此该配置不保证精确复现实际环境。依赖无法加载及 AF 部署产物被替换仍报错。AFX 在部署后更新不会单独阻止 replay，实际加载身份写入 report.json。

`deploy --issue-id F152` 关联领取上下文，在编包期间续租。部署完成后，通过独立的 `issue verify` 命令提交平台验证。

```bash
# 离线校验并生成本地快照
af-forge deploy --dry-run

# 查看远端任务日志
npussh <environment> 'tail -n 100 /remote/workflow-state/<job-id>/job.log'
```

同步包含受版本控制文件及未忽略的新文件，排除凭据、agent 私有配置、构建目录和依赖缓存。每次生成完整目录快照及哈希清单。删除操作仅处理上一份托管清单拥有的源码文件。`.af-workflow` 标识远端源码归属，手工修改托管文件会阻止同步。

同一源码目录的同步/编译串行，安装与受管理的复测通过锁协调。工具链或 CANN 指纹变化时应检查并移走不兼容的专用 build 缓存。AF 覆盖安装失败会使当前部署失效；Python 更新支持锁文件回滚，AF/CANN 安装失败需检查现场并恢复安装。

## AFX 平台修复流程

启用模板中的 platform 节，填写 HTTPS 地址、agent_id、本地及远端 token 文件路径。两端使用同一平台用户的 Bearer token。凭据文件放在源码目录外，远端文件权限需为 0600。

### 直接领取平台编号

平台提供 `/api/issues` 且 agent token 拥有 `issue` 权限时，可以直接使用功能、精度或性能问题编号（F、A、P 加 1 至 9 位数字）。

```bash
af-forge issue claim --issue-id F152
af-forge issue preview --issue-id F152
af-forge issue renew --issue-id F152
af-forge deploy --issue-id F152 --request-id deploy-F152-1
# 用 status --job-id 确认这次部署成功后提交
af-forge issue verify --issue-id F152 --job-id <deploy-job-id>
```

领取成功后终端显示问题摘要。完整回执保存在本机 `issues/<issue_id>.json`；编号问题的 preview 默认输出缩进 JSON，包含失败摘要和去重后的全部 nnsmith seed。若领取回执被截断，preview 会从平台分页读取当前受影响的用例。加 `--full` 可查看领取时保存的复现详情和相关知识。编号领取可显式传 `--request-id`；省略时 CLI 在发送前将生成的 ID 写入 stderr，重试使用同一个 ID。修复期间按租约到期时间提前续租，关联部署期间 workflow 自动续租。验证以实测 AF 版本及 pyautofuse.so 哈希提交，返回 `data.round_no`。平台按芯片和包身份调度已注册客户端。

### 平台分配与结果查询

省略 `--issue-id` 时，平台分配一个待修复问题；没有任务时返回 `data=null`。

```bash
af-forge issue claim --request-id claim-001
# 使用 verify 返回的 round_no
af-forge issue status --issue-id F152 --round-no 1
af-forge issue wait --issue-id F152 --round-no 1 --timeout 3600
```

verify 在远端核对部署记录与实际加载的 AF，向 `/api/issues/{id}/verify` 提交 lease_id、fix_version、fix_sha256 和 request_key。job 的 platform.json 保存固定请求和回执。网络失败后重试同一部署会复用请求键；不同修复使用新的部署 job。

status/wait 查询指定 verify 轮次及紧随其后、包身份一致的 regress 轮次。回归仍在执行时不会把验证通过视为整个流程通过；后续修复的轮次不会覆盖本次结果。失败或超时返回错误码及轮次详情，超时后可继续查询同一 round_no。修复总结由平台处理。

## AFX 常驻客户端

Forge 在本地控制远端任务；AFX 官方客户端在 NPU 上独立运行，负责心跳、上传、复测领取和平台更新。Forge 不提供客户端管理命令，不代理常驻进程，也不执行 stop 或自动重启。

在 NPU 上使用目标环境启动，例如（按实际配置修改路径）：

```bash
source /mnt/workspace/af-workflow/Ascend/cann/set_env.sh
source /mnt/workspace/af-workflow/venv/bin/activate
export AUTOFUSEX_HOME=/mnt/workspace/af-workflow/afx
mkdir -p "$AUTOFUSEX_HOME/results"
afx platform client run --endpoint https://autofuse.online \
  --token-file /absolute/path/client.token \
  --results "$AUTOFUSEX_HOME/results/results.sqlite" --retest
```

SQLite 数据库由 AFX 创建，不必手工建立空文件。`--retest` 启用平台复测任务领取；客户端需报告与 verify 提交相符的后端版本、哈希和芯片信息。包更新、注册和调度遵循 AFX 自身行为。Forge 的环境锁仅协调 Forge 任务，不代表独立客户端也持有这些锁。

## 独立 seed 复跑

```bash
af-forge replay \
  --job-id <deploy-job-id> --seed <seed>

af-forge replay \
  --job-id <deploy-job-id> --seeds-file /absolute/path/seeds.txt
```

seed 文件每行一个 seed。replay 校验当前部署、环境清单和加载库，创建独立 HOME、缓存和结果数据库。单 seed 使用第一张配置卡，批量模式使用配置的 workers、设备列表和 timeout。

Shape 数量由 AFX 解码 seed 得到：静态为 1，动态为 2–8。结果通过 `fuzz maintenance results` 分页读取及查询 attempt 详情，不直接读取 SQLite 表。

通过要求为执行命令成功、CPU/AF-MLIR attempt 均为 ok、seed/backend/shape 完整且无重复。AF-MLIR correctness 必须为 pass，CPU 参考后端允许 not_checked 或 pass。unsupported、缺失结果和目标后端未比较均判失败。

独立 replay 默认只保存结果。启用 upload 或传 --upload 时使用 upload.sync_config_path 请求 AFX 同步，--no-upload 可覆盖。report.json 中 upload=pending 表示同步已请求，服务端接收情况需另查。平台客户端会上传自己领取的验证任务结果。

## 环境清单、产物与清理

| 位置 | 内容与用途 |
|---|---|
| 本地 `requests/` | request-id 到冻结任务的幂等索引 |
| 本地 `issues/<id>.json` | 平台 issue 领取响应、冻结目标及租约 |
| job 的 `config.yaml` | 有效配置；环境操作成功或部署/复跑环境校验通过后含实际版本、日期、哈希和内嵌依赖锁 |
| job 的 `request.json` / `launch.json` / `status.json` | 请求、远端启动凭据、阶段与终态 |
| job 的 `source.json` | 源码清单、权限、链接、哈希及快照清理结果 |
| job 的 `deployment.json` / `packages/` | 部署来源、加载证据及 AF run 包 |
| job 的 `report.json` | 独立 replay 的资源、结果及同步状态 |
| job 的 `result.json` | 环境操作结果；环境更新包含组件和回滚信息 |
| job 的 `platform.json` | 平台 verify 固定请求及回执 |
| job 的 `python-dependencies.json` / `cann-bootstrap.json` | 依赖检查和 CANN 安装证据 |
| job 的 `job.log` / `uv.log` | 操作日志 |
| `workflow-env-state/current.json` | 当前环境清单，供环境一致性校验及更新使用 |
| `workflow-env-state/wheels/` / `locks/` | 按哈希保存 torch_npu/AFX wheel 和依赖锁 |

源码快照 snapshot/ 为普通目录树。确认远端启动后清理本地副本，远端同步校验成功后清理远端副本。状态不明确时保留现场，dry-run 保留快照。源码清单和部署引用继续用于校验，删除快照后需另行保留源码才能恢复未提交修改。

完整 CANN 下载包在安装成功后清理，失败时保留。AF run 包、共享 wheel、锁文件和任务记录持续保留，按需要归档。当前部署注册表通过路径和 SHA-256 引用 deployment.json，后者引用 source.json；后续复跑依赖这些文件。

## 从冻结配置重建环境

将远端 job 的 config.yaml 取回本地，执行 `init --config /absolute/path/config.yaml`。目标 venv 要求尚不存在。异地重建时调整运行、状态和源码路径，保留 environment_lock 和包来源。

恢复使用内嵌 Python 依赖锁、实际 Daily 日期、CANN 发布号及包哈希。基础 Python 版本必须匹配，包源或共享 wheel 需仍可访问。外部 CANN 缺少安装收据时，只记录可探测的版本信息，恢复需准备相应安装。源码、seed 和运行结果按各自产物保存。

## 验证工具本身

```bash
python3 -m pytest tests -q
```

测试使用临时目录、Git/rsync 和构建、平台、设备接口夹具，覆盖同步、幂等、更新回滚、环境冻结、结果判定和平台验证轮次。真实编包、NPU 执行及平台联调需在配置的目标环境验证。

指定 AFX 源码运行契约测试：

```bash
AFX_SOURCE=/absolute/path/Autofuse-Debugger python3 -m pytest tests/test_afx_contract.py -q
```

契约测试需要 pytest、PyYAML、jsonschema；调用真实的 AFX seed、结果 CLI 和修复服务逻辑，使用临时数据库，不连接生产平台。

## MCP 接入

MCP 客户端配置示例（命令需在客户端 PATH 中，或使用 af-forge-mcp 的绝对路径）：

```json
{
  "mcpServers": {
    "af-forge": {
      "command": "af-forge-mcp",
      "args": ["--config", "/absolute/path/config.yaml"]
    }
  }
}
```

提供两个工具：`workflow_help(command)` 查询命令参数；`workflow_run(command, arguments, config)` 执行命令并返回原有 JSON 响应，失败同时标记 MCP `isError`。例如 `{"command":"deploy","arguments":["--request-id","fix-001"]}`，随后用 `{"command":"status","arguments":["--job-id","<job-id>"]}` 查询。`arguments` 是参数数组，不经过 shell。

`init/update` 通过 MCP 自动使用 `--detach`，不会占住会话等待远端安装；其他命令保持 CLI 行为。取消 MCP 调用停止本地 CLI 子进程，已提交的远端任务仍可通过 job ID 查询。标准输出仅用于 MCP 协议，诊断日志写到标准错误。只有公开命令可通过 MCP 调用；内部 worker 入口不开放。MCP 不额外增加平台行为或自动提交 verify。

## 构建 wheel

```bash
uv venv --python 3.11
uv pip install -e '.[dev]'
.venv/bin/python -m pytest tests -q
.venv/bin/python -m build
```

产物位于 `dist/`。wheel 包含远端运行模块、包源配置和 YAML 模板，不包含用户配置、token、源码快照或本地状态。本工具沿用 [CANN Open Software License Agreement Version 2.0](LICENSE)。

### 命令分组

`af-forge help` 显示顶层命令；`af-forge issue --help` 显示问题处理操作。旧的连字符子命令不再作为 CLI 入口。

MCP 使用相同命令路径：`workflow_help({"command":"issue"})` 查看分组，`workflow_help({"command":"issue verify"})` 查看操作参数；`workflow_run({"command":"issue claim"})` 执行操作。参数放在 `arguments` 数组中，例如 `{"command":"issue verify","arguments":["--issue-id","F152","--job-id","<job-id>"]}`。JSON 响应的 `command` 使用空格分隔路径；任务记录中的内部操作标识保持不变。
