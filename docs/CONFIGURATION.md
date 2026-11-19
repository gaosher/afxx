# AF Forge 配置

本文说明配置选择、字段默认值、环境初始化和恢复约束。日常命令流程见 [README](../README.md)，完整示例见 [带注释的 YAML 模板](../af_forge/resources/config.example.yaml)。

## 创建和选择配置

```bash
af-forge config init --source /absolute/path/graph-autofusion --output ~/.config/af-forge/config.yaml
```

`config init` 自动填写 `local.source_dir`，保留模板注释并拒绝覆盖已有文件。填写 `connection.environment`、`workflow_root`、`build.llvm_build_dir` 和 `build.lld_bin`；根据需要修改 Python/CANN 选项。私人配置不要纳入版本控制。

配置按 `--config`、`AF_FORGE_CONFIG`、当前目录 `af-forge-config.yaml`、用户配置目录 `~/.config/af-forge/config.yaml` 的顺序选择（支持 `XDG_CONFIG_HOME`）。源码仓库由 `local.source_dir` 指定，命令可在任意目录执行。

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

## 环境一致性与冻结配置

deploy/replay 的配置、当前环境清单与实际包版本不一致时，输出含 expected/actual 的 WARNING 并继续；告警保留在 job.log 中。有差异时保留提交的 config.yaml，不写入旧清单的环境锁，因此该配置不保证精确复现实际环境。依赖无法加载及 AF 部署产物被替换仍报错。AFX 在部署后更新不会单独阻止 replay，实际加载身份写入 report.json。

## 从冻结配置重建环境

将远端 job 的 config.yaml 取回本地，执行 `init --config /absolute/path/config.yaml`。目标 venv 要求尚不存在。异地重建时调整运行、状态和源码路径，保留 environment_lock 和包来源。

恢复使用内嵌 Python 依赖锁、实际 Daily 日期、CANN 发布号及包哈希。基础 Python 版本必须匹配，包源或共享 wheel 需仍可访问。外部 CANN 缺少安装收据时，只记录可探测的版本信息，恢复需准备相应安装。源码、seed 和运行结果按各自产物保存。
