# AFXX — AFX Extensions

使用 `afxx --version` 查看当前安装版本，`afxx --help` 查看命令列表。

AFXX 是面向本地 agent 的 AFX 开发与问题修复工具包：准备 NPU 环境，同步本地 graph-autofusion 源码，编包安装，再通过 AFX managed client 复跑 seed 和提交平台验证。CLI 与 MCP 共用实现。

下面从环境初始化开始。假定已安装 AFXX、完成 `npussh login` 并填写 `~/.config/afxx/config.yaml`；首次使用先看[安装与配置](#安装与配置)。所有命令默认读取该配置，其他配置通过 `--config /absolute/path/config.yaml` 指定。

## 1. 初始化环境：env init

```bash
afxx env init
afxx env check
```

`env init` 自动上传配置和执行工具，在远端准备 Python 3.11、uv、专用 venv、torch、匹配的 torch_npu、AF 所需依赖和 AFX。不需要手动同步配置。

CANN 按配置检查：空的安装目录触发下载安装；非空但不完整的目录报错，不直接覆盖。已有完整 CANN 时复用。LLVM 和 LLD 需要提前在远端准备并填写路径。AFXX 不在这一步编译本地 graph-autofusion，源码修改通过后面的 deploy 安装。

init 默认实时输出远端日志，结束后返回 JSON 摘要。停止本地日志查看不会停止远端任务。需要异步执行时：

```bash
afxx env init --request-id env-init-001 --detach
afxx status --request-id env-init-001
```

检查外层 `ok` 和任务 `state`：提交成功不代表环境已初始化成功，应等到 `state=success`。失败时按返回的日志和报告定位，不直接删除现场后重试。

配置平台连接后，init 还会处理平台凭据准备；非交互运行需提前提供本地 token 文件。凭据路径、Python/CANN 版本和失败恢复规则见[配置说明](docs/CONFIGURATION.md#初始化和更新环境)。

## 2. 查询和更新环境：env update

```bash
# 只查询当前版本，不更新包
afxx env update

# 只更新 AFX
afxx env update --afx latest

# 更新 torch_npu 和 AFX
afxx env update --torch-npu latest --afx latest

# 更新成功后，将实际版本回写本次配置
afxx env update --afx latest --save-config
```

`env update` 以当前环境清单为基础，只更新命令明确指定的组件。配置中的初始化版本不会把其他包重新装回去；默认不更新 CANN，也不修改本地配置文件。

需要更新 torch 或 CANN 时显式指定：

```bash
# torch 变更必须同时选择 torch_npu，Daily 匹配目标 torch 版本
afxx env update --torch 2.10.0+cpu --torch-npu latest

# 只更新 Toolkit；或同时更新同批次 OPS
afxx env update --cann latest
afxx env update --cann latest --ops
```

更新不会自动停止或重启 client。若环境正在被使用，先协调在途任务，通过 `client stop` 停止 client，再更新；完成后按需要启动。Python 更新失败会尝试回滚，CANN 安装失败不保证自动回滚，需保留报告检查。具体限制见[环境更新说明](docs/CONFIGURATION.md#初始化和更新环境)。

## 3. 同步源码、编包并安装：deploy

本地修改完成后执行：

```bash
afxx deploy --request-id deploy-001
afxx status --request-id deploy-001
```

默认实时跟随远端日志，完成后返回最终结果；日志输出到 stderr，stdout 保留 JSON 回执。`Ctrl+C` 只停止查看，不终止远端部署，可继续用 `status` 查询。后台提交使用 `afxx deploy --detach`；MCP 自动使用后台提交。

这一条 deploy 完成以下步骤：

1. 采集远端环境，检查构建和安装依赖。
2. 冻结本地源码快照，通过 npursync 同步到专用远端源码目录。
3. 执行 `bash build.sh --pkg --enable-mlir -j <build.jobs>`，默认并行度为 8。
4. 校验 AF run 包并升级安装，验证加载的 pyautofuse、adapter、codegen 与本轮构建产物一致。

同步包含未提交修改和未忽略的新文件，**不需要 git push**。凭据、agent 私有配置、构建目录及依赖缓存不会作为源码同步。源码目录和编译目录在远端复用；安装前仍会检查环境占用，不会强行覆盖正在使用的库。

查询到 `state=success` 后才能使用这次部署。运行较久时可等待状态变化：

```bash
afxx status --request-id deploy-001 --wait --timeout 30
# 后续带上上次返回的 cursor
afxx status --request-id deploy-001 --wait --since <cursor> --timeout 30
```

相同部署 request_id 用于重试原提交；修改代码后再次部署应使用新的 ID，例如 `deploy-002`。可以先运行 `afxx deploy --dry-run` 离线检查并生成源码快照。

## 4. 管理现有 AFX client

```bash
afxx client status
afxx client start
afxx client stop --timeout 300
afxx client restart --timeout 300
```

AFXX 复用 AFX 当前解析出的 client 目录，包括 `AUTOFUSEX_HOME`、active-home 指针或默认目录；不会为每次 replay 创建第二个 client。首次初始化需要配置平台连接和远端 `platform.client_token_file`，已有 client 可复用保存的凭据。

AFX 负责执行会话、任务队列、心跳、上传和平台复测领取。`start` 可能同时开启随机 fuzz，并非仅监听复测。`stop` 等待在途任务结束，超时不强杀；检测到其他 client 时报告冲突。

**deploy 更新磁盘上的包，不会刷新已运行的 worker。** 使用新 AF 包复测前，通过 `client restart` 刷新执行进程，再确认状态。若安装前需要释放环境，先 stop，等部署成功后再 start。不能仅凭提交端探测到新包就认定 client 使用了新包。

## 5. 复跑 seed、查询和对比：replay

```bash
afxx replay --request-id repair-F152 --seed <seed>

# 批量复跑：文件每行一个 seed
afxx replay --request-id repair-F152 --seeds-file /absolute/path/seeds.txt
```

单 seed 和批量均调用 `afx replay`，交给已有 managed client 排队执行。AFXX 不增加本机历史门禁，是否支持某个 seed 由实际 AFX 接口判断；上游错误会返回给调用方。

同一修复流程复用 `request_id`，每次 replay 自动新增一个 `run`。不需要 `--new-run`，也不引用 deploy ID。断线后先查记录，重复调用 replay 会再次提交。

```bash
afxx replay list --request-id repair-F152
afxx replay show --request-id repair-F152          # 默认最新一次
afxx replay show --request-id repair-F152 --run 1
afxx replay compare --request-id repair-F152 --runs 1 2
```

AFXX 保存 AFX 的回执，按本次 task/attempt ID 跟踪结果，不混入共享会话的历史或并行结果。入队成功不是验证通过；终态查看 `state`、`passed` 和各后端/shape 的结果。CPU 作为参考时允许 `not_checked`，其他后端要求正确性通过。

设备、后端、线程和上传策略由 AFX 管理，AFXX 的 replay 资源配置不作为逐任务覆盖。`replay show` 的 `environment` 来自执行结果；`submission_environment` 是提交端快照，两者不能混为一谈。

`compare` 按完整 seed、backend、shape_index 对齐，区分变化、新增、缺失和无法对齐，不会自动把“少了失败记录”判为修复成功。

### 当前定位能力与 dump 限制

可以读取本次结果、图元数据、错误栈和执行环境。**逐任务开启并持久保存完整 dump 尚未接通**，不能保证拿到逐 pass IR、kernel、tiling 或输入输出张量；worker 临时编译缓存也可能被清理。

以下选项会明确返回 `MANAGED_REPLAY_UNSUPPORTED`：

- `--dump` 或 `replay.dump: true`。
- `--upload`、`--no-upload` 或 `upload.enabled: true`；上传由现有 client 管理。

不会静默忽略，也不会回退到独立的旧执行入口。定位必须依赖完整 dump 时，应记录证据缺口，等待 AFX 的逐任务采集接口。

终态查询返回 `evidence_path`，下载本次 AFXX 报告和日志，不复制整个共享 client 数据库或其他任务的缓存。取证方法见[复现证据清单](skills/afx-issue-repair/references/replay-evidence.md)。

## 6. 完成 issue 修复与平台验证

先配置[平台连接](docs/CONFIGURATION.md#平台连接)，本地 agent token 需要相应 issue 权限。

### 领取并查看问题

```bash
afxx issue list
afxx issue claim --issue-id F152 --request-id claim-F152
afxx issue preview --issue-id F152
# 修复期间按租约到期时间提前续租
afxx issue renew --issue-id F152
```

claim 必须指定 issue_id。preview 默认显示失败摘要和全部去重 seed；需要更多内容时加 `--full`。基础复现、定位和修复过程遵循[修复 skill](skills/afx-issue-repair/SKILL.md)。

### 多轮部署和复跑

每轮修改使用新的部署 ID，replay 继续使用同一个分组 ID：

```bash
afxx deploy --issue-id F152 --request-id deploy-F152-1
afxx status --request-id deploy-F152-1
# 确认部署成功，再刷新 client
afxx client restart
afxx replay --request-id repair-F152 --seed <seed>
afxx replay show --request-id repair-F152
```

需要继续修复时，下一轮部署用 `deploy-F152-2`，replay 仍用 `repair-F152`。关联 issue 的部署会在构建期间续租。基线、候选版本与每次复跑结果应分别保留，不能把“任务已结束”当作问题已修复。

### 提交 verify 并检查回归

```bash
# 确认最终部署成功；verify 前刷新 client
afxx client restart
afxx issue verify --issue-id F152 --job-id deploy-F152-2

# 使用 verify 返回的 round_no
afxx issue verify-check --issue-id F152 --round-no <round_no> --wait --timeout 30
```

`issue verify` 当前仍通过 `--job-id` 引用部署，传入对应部署的 request_id 值；普通任务查询已使用 `status --request-id`。提交 verify 只代表平台已受理，还需确认验证及关联回归的终态。查询超时可继续查同一 round_no，不要重新提交验证。

查看具体失败任务和执行证据：

```bash
afxx issue tasks --issue-id F152 --round-no <round_no>
afxx execution show --exec-uuid <exec_uuid>
```

不再处理该 issue 时，可以显式释放租约：

```bash
afxx issue release --issue-id F152 --request-id release-F152
```

修复总结先生成本地 `summary.yaml` 并检视，之后再显式发布：

```bash
afxx issue submit-summary --issue-id F152 \
  --summary-file /absolute/path/summary.yaml --request-id summary-F152
```

发布需要平台状态、租约和权限满足要求；不会由 verify 自动触发。总结格式见[模板](skills/afx-issue-summary/assets/summary.yaml)，其他平台查询见[命令参考](docs/afxx-commands.md)和[API 实现对照](docs/PLATFORM_API_MAPPING.md)。

## 7. 接入 agent：Skill 与 MCP

安装随 AFXX 发布的修复和总结 skill：

```bash
afxx skills install --agent codex
```

安装到 `~/.agents/skills/afx-issue-repair` 和 `afx-issue-summary`。相同内容重复安装会跳过；已有内容不同则返回 `SKILL_CONFLICT`，保留旧文件，需先备份并移走旧目录再安装。

调用示例：`使用 $afx-issue-repair 修复 AFX issue F152，验证和回归通过后 commit，等待检视。` 需要完整 dump 的定位目前仍会受上一节的接口限制。

MCP 是可选入口，agent 也可以直接使用 CLI。安装可选依赖并启动 stdio 服务：

```bash
python3 -m pip install 'afxx[mcp]'
afxx mcp
```

实际使用时由 MCP 客户端启动 `afxx mcp`，无需在 NPU 上额外部署 MCP 服务。采用 `mcpServers` 格式的客户端可配置：

```json
{
  "mcpServers": {
    "afxx": {
      "command": "/absolute/path/to/afxx",
      "args": ["mcp"]
    }
  }
}
```

需要其他配置时，args 增加 `--config /absolute/path/config.yaml`。客户端运行环境必须能找到 npussh 等本地工具。

常用结构化工具包括 `afxx_deploy`、`afxx_replay`、`afxx_job_status`、`afxx_client`、`afxx_issue` 和 `afxx_issue_status`，其他命令可通过 `afxx_run` 调用。

replay 直接返回 request_id/run，用 `replay show` 查询。其他需要本机长操作回执的命令先查 `afxx_operation_status`；调用完成后仍要继续检查远端任务或平台轮次。参数和恢复规则见[MCP 调用说明](docs/afxx-commands.md#11-mcp-调用)。

## 安装与配置

本地需要 Python 3.11+、Git、Node.js 20+、npussh/npursync、OpenSSH 和 rsync。npursync 随 npussh 提供；完整安装说明见[本地依赖](docs/LOCAL_DEPENDENCIES.md#本地启动环境)。

```bash
python3 -m pip install /absolute/path/afxx-1.0-py3-none-any.whl
npussh login
npussh list
afxx config init --source /absolute/path/graph-autofusion
```

编辑生成的 `~/.config/afxx/config.yaml`，填写 NPU 环境、workflow_root、远端 LLVM/LLD 路径，以及所需 Python/CANN 版本。`config init` 不覆盖已有文件，私人配置不应提交到仓库。

```bash
afxx doctor
```

doctor 检查配置、本地依赖和登录状态。通过后回到 [env init](#1-初始化环境env-init)。远端目录由 workflow_root 推导，CANN 路径可覆盖；字段和默认值见[配置说明](docs/CONFIGURATION.md)及[YAML 模板](afxx/resources/config.example.yaml)。

## 任务记录与清理

| 位置 | 内容 |
|---|---|
| 本地 `local.state_dir/requests/` | 部署等提交的幂等记录 |
| 本地 `local.state_dir/replay-groups/` | request_id 下的 replay 历史索引 |
| 本地 `local.state_dir/issues/` | issue 领取记录及修复材料 |
| 远端 `workflow-state/<执行ID>/` | 冻结配置、请求、状态、环境、日志、报告及本轮 AF run 包 |
| AFX 当前 home 的 `runs/` | managed client 管理的执行队列与结果 |
| 本机返回的 `evidence_path` | 本次 replay 下载的报告与日志 |

保留本地索引才能跨 CLI/MCP 查询同一组历史。源码快照是普通目录树：本地在确认远端启动后清理，远端在同步校验成功后清理；失败或状态不明确时保留现场。删除快照不等于归档未提交源码，原始修复仍需通过 Git 等方式保存。

全量 CANN 包下载到远端 `/tmp`，安装成功后清理，失败保留。AF run 包、任务报告和环境恢复所需记录持续保留。本机 evidence_path 也在 `/tmp`，重要证据需另行归档。环境重建见[冻结配置说明](docs/CONFIGURATION.md#从冻结配置重建环境)。

## 其他能力与开发

### 独立随机 fuzz

需要单独运行有限数量的随机用例时：

```bash
afxx fuzz run --count 100 --request-id fuzz-001
afxx status --request-id fuzz-001
afxx fuzz status --job-id fuzz-001
afxx fuzz stop --job-id fuzz-001
```

这是独立的随机测试能力，使用 `<workflow_root>/fuzz/`，不属于上述 managed replay 修复主线。省略 count 会持续运行；更新环境或部署前应停止相应任务。status 的 success 表示进程正常结束，不代表全部随机用例通过。详见[命令参考](docs/afxx-commands.md)。

### 测试与构建

```bash
python3 -m pip install -e '.[dev,mcp]'
AFX_SOURCE=/absolute/path/Autofuse-Debugger python3 -m pytest tests -q --require-afx-contracts
python3 -m build
```

未设置 AFX_SOURCE 的日常测试会跳过真实契约；发布前必须使用 `--require-afx-contracts`，不能将跳过当成接口兼容通过。测试使用临时数据，不连接生产平台；真实编包和 NPU 执行需单独验证。模块划分见[设计说明](docs/DESIGN.md)。

wheel/sdist 位于 `dist/`，包含执行模块、模板和 skill，不包含用户配置、token 或任务现场。

### 发布

[publish.yml](.github/workflows/publish.yml) 在推送 `v*` tag 时执行测试、构建、安装验证并发布 PyPI。首次发布需配置 GitHub 的 `pypi` environment 和对应 PyPI Trusted Publisher；后续递增 pyproject.toml 版本，创建与之匹配的 tag。AFX 契约版本固定在工作流的 AFX_REVISION，更新后需重新验证。

完整接口见[命令参考](docs/afxx-commands.md)、[CLI/MCP/API 对照](docs/PLATFORM_API_MAPPING.md)和[Agent 调用约定](docs/AGENT_USAGE.md)。本项目采用 [Apache License 2.0](LICENSE)。
