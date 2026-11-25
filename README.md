# AFXX — AFX Extensions

## 简介

AFXX 是面向本地 agent 的 AFX 开发与问题修复工具包，主要功能包括：

1. 准备 afx 运行环境 (cann, torch & torch_npu, afx)
2. 同步本地 graph-autofusion 源码，编包以及安装
3. 提供一套 skill，完成 afx 平台聚类 issue 的 领取 -> 修复 -> 验证 -> 总结 闭环

> 命令在本机执行，AFXX 通过 npussh/npursync 操作远端 NPU 环境。无需在远端额外安装 AFXX；AFX 和编译运行环境由 env init 准备。CLI 与 MCP 使用同一套实现。

## AFXX 安装

本地需要 Python 3.11+、Git、Node.js 20+、npussh/npursync、OpenSSH 和 rsync。npursync 随 npussh 提供；完整安装说明见[本地依赖](docs/LOCAL_DEPENDENCIES.md#本地启动环境)。

```bash
python3 -m pip install --upgrade afxx
afxx --version
```

## 快速开始

完成以下三步，即可让 agent 使用 Skill 执行修复闭环。手动操作或查询时参阅[功能介绍](#功能介绍)。

### 1. 创建配置文件

```bash
# 通过 npussh 登录 npu 环境
npussh login
npussh list

# 创建配置文件，默认位置 ~/.config/afxx/config.yaml
afxx config init --source /absolute/path/graph-autofusion

# doctor 检查配置、本地依赖和登录状态
afxx doctor
```

编辑 `~/.config/afxx/config.yaml`，确认以下配置：

- **远端环境**：NPU 环境名称、工作目录 `workflow_root`，以及编译使用的 LLVM/LLD 路径。
- **软件版本**：确认 Python、torch、torch_npu、AFX 和 CANN 的配置；其余字段通常保留默认值。远端目录默认放在 `workflow_root` 下，也可指定已有 CANN 目录。

如需领取 issue、上传结果或提交验证，还需配置 **AFX 平台地址和 token**。交互执行 `env init` 时会引导准备 token；非交互式运行需先准备好本地 token 文件。

完整字段说明见[配置说明](docs/CONFIGURATION.md)，默认值见[YAML 模板](afxx/resources/config.example.yaml)。

>`config init` 不覆盖已有文件。

### 2. 初始化环境

```bash
afxx env init
```

命令自动上传配置和执行工具，准备远端 Python、CANN 和 AFX 环境，并实时显示日志。等待返回 `ok=true` 且 `state=success` 后再继续；无需手动同步配置。检查、更新及断线恢复见[环境管理](#环境管理)。

### 3. 使用修复 Skill

先在本机安装 Skill：

```bash
afxx skills install --agent codex
```

然后在 Codex 对话中输入 Prompt：

```text
使用 $afx-issue-repair 修复 <issue ID>，完成验证后 commit 修复代码并生成 summary.yaml
```

Skill 会完成“领取 → 基线部署与复现 → 定位修复 → 部署验证 → 关联回归”，交付修复 commit、验证结论和 `summary.yaml`，不会自动 push 或发布总结。无需先手动 deploy。

Skill 可以直接使用 AFXX CLI，**不依赖 MCP**；已接入 MCP 时优先使用结构化工具。安装与接入细节见[Agent 接入](#agent-接入)。

## 功能介绍

- [环境管理](#环境管理)
- [源码部署](#源码部署)
- [客户端管理](#客户端管理)
- [用例复跑与定位](#用例复跑与定位)
- [平台问题处理](#平台问题处理)
- [平台数据查询](#平台数据查询)
- [独立随机测试](#独立随机测试)

CLI 保持结构化 JSON：交互终端自动缩进换行，管道、重定向及 MCP 保持紧凑输出。日志和进度走 stderr，结果 JSON 走 stdout；stderr 有内容不代表失败，应结合退出码、`ok` 和任务终态判断。

### 环境管理

#### 1. 创建本地配置
```bash
# 创建配置文件，默认位置 ~/.config/afxx/config.yaml
afxx config init --source /absolute/path/graph-autofusion

# 检查配置、本地依赖和登录状态。
afxx doctor

`config init` 不覆盖已有配置
所有命令默认读取 `~/.config/afxx/config.yaml`，使用其他配置时需要追加 `--config /absolute/path/config.yaml`。
```
#### 2. 初始化远端环境
`env init` 自动准备 CANN环境、Python 3.11、uv、专用 venv、torch、匹配的 torch_npu、AF 所需依赖和 AFX。
```bash
afxx env init [--detach]
```
- init/update 分别自动分配 `ENV-INIT-` / `ENV-UPDATE-` 加 12 位随机十六进制后缀的任务 ID；后续查询使用返回的 ID。
- `--detach`参数设置不跟随远端日志
- 提交成功不代表初始化完成，应确认任务 `state=success`。

通过 `status` 命令查询当前初始化任务的执行状态

```bash
afxx status --request-id <返回的ENV开头ID>
```

`env check` 同步返回检查结果，不生成 ID、任务目录或幂等记录；断线后直接重跑。它检查虚拟环境、Python 依赖导入、AFX 接口及最低版本要求、pip 依赖冲突，并采集实际环境。
```bash
afxx env check
```
#### 3. 按需更新

`env update` 以当前环境清单为基础，只更新命令明确指定的组件。默认不修改本地配置文件。
```bash
# 只查询当前版本，不更新包
afxx env update

# CANN 更新，不指定日期参数时默认latest；加 --ops 同时更新 OPS
afxx env update --cann [YYYYMMDD] [--ops]

# AFX 更新到最新版本
afxx env update --afx

# torch_npu 更新，不指定日期时默认latest
afxx env update --torch-npu [YYYYMMDD]

# torch 必须指定版本，同时更新匹配的 torch_npu
afxx env update --torch 2.10.0+cpu --torch-npu [YYYYMMDD]

# 支持多个组件组合更新
afxx env update --afx --torch-npu
afxx env update --cann --ops --afx

# 成功后将实际版本回写配置
afxx env update --afx --save-config
```
- `--cann`、`--torch-npu` 不带值时默认 `latest`，也可指定日期（`YYYYMMDD`）。
- `--torch` 必须带确切版本；`--ops` 必须与 `--cann` 一起使用。

> - 更新不会自动停止或重启 client。若环境正在被使用，先协调在途任务，通过 `client stop` 停止 client，再尝试更新
> - Python 更新失败会尝试回滚，CANN 安装失败不保证自动回滚，需保留报告检查。具体限制见[环境更新说明](docs/CONFIGURATION.md#初始化和更新环境)


### 源码部署

本地修改完成后执行：
```bash
afxx deploy [--coordinate-client] [--request-id deploy-001]

# 可先运行离线检查并生成源码快照
afxx deploy --dry-run
```
这一条 deploy 完成以下步骤：

1. 采集远端环境，检查构建所需的 Python 依赖。
2. 冻结本地源码快照，通过 npursync 同步到专用远端源码目录。
  - 同步包含未提交修改和未忽略的新文件。
  - 凭据、agent 私有配置、构建目录及依赖缓存不会作为源码同步。
3. 执行 `bash build.sh --pkg --enable-mlir -j <build.jobs>` 编包。
  - 源码目录和编译目录在远端复用
4. 校验 AF run 包并升级安装，验证加载的 pyautofuse、adapter、codegen 与本轮构建产物一致。
  - 安装前仍会检查环境占用，不会强行覆盖正在使用的库。

>默认实时跟随远端日志，完成后返回最终结果。`Ctrl+C` 只停止查看，不终止远端部署，可继续用 `status` 查询。后台提交使用 `afxx deploy --detach`；MCP 自动使用后台提交。

`status` 命令查看当前 deploy 任务状态：
```bash
afxx status --request-id deploy-001
```
查询到 `state=success` 后才能使用这次部署。运行较久时可等待状态变化：
```bash
afxx status --request-id deploy-001 --wait --timeout 30
# 后续带上上次返回的 cursor
afxx status --request-id deploy-001 --wait --since <cursor> --timeout 30
```
相同deploy request_id 用于重试原提交；修改代码后再次部署应使用新的 ID，例如 `deploy-002`。省略 `--request-id` 时每次自动生成新 ID。cursor 表示上次查询到的状态，`--wait --since` 等待相对该状态的变化；

部署需要暂停并恢复目标 AFX 客户端时使用 `--coordinate-client`，详见[部署客户端协调](docs/afxx-commands.md#deploy-客户端协调)。原来停止的客户端保持停止，运行会话重新启动，不保证原队列续跑。

### AFX客户端管理

AFXX 管理远端当前用户唯一存活的 client，采用其实际 home；发现多个存活 client 时直接报错。supervisor 与其管理的 daemon 算同一个 client，执行会话停止但 daemon 存活时仍算在线。任务调度、心跳、上传和平台复测领取由 AFX 负责。

#### 1. 查询状态：status

```bash
afxx client status
```

向 AFX daemon 查询客户端及执行会话状态。没有存活 client 时返回 `stopped`、`daemon_running=false`；daemon 存活但没有运行中的会话时通常为 `idle`。

#### 2. 启动客户端：start

```bash
afxx client start
```

有存活 client 时复用其实际 home；没有时由 AFX 解析当前 home（`AUTOFUSEX_HOME`、active-home 或默认目录）。已有目录沿用 AFX 保存的身份和配置，首次初始化才使用 AFXX 的平台凭据及设备配置。启动进程使用 AFXX 配置的 Python/CANN 环境。

start 可能同时开启随机 fuzz，并非只启动复测监听。

#### 3. 停止客户端：stop

```bash
afxx client stop --timeout 300
```

先请求 AFX 停止执行会话，等待在途任务结束，再停止 daemon 并确认相关守护进程退出。停止的是整个 client，不只是暂停 fuzz；没有存活 client 时直接返回 `stopped`。

`--timeout` 可省略，默认等待 300 秒；超时返回错误，不强杀进程。

#### 4. 重启客户端：restart

```bash
afxx client restart --timeout 300
```

先完成停止流程，再使用同一 home 和 AFXX 配置的 Python/CANN 环境启动。没有存活 client 时返回 `PLATFORM_CLIENT_NOT_RUNNING`，应改用 start。`--timeout` 可省略，默认 300 秒。

### 用例复跑与定位

```bash
afxx replay [--request-id issue-F001-repair] --seed <seed>

# 批量复跑：文件每行一个 seed
afxx replay [--request-id issue-F001-repair] --seeds-file /absolute/path/seeds.txt
```

AFXX 使用 `afx fuzz replay <seed> --save`，不强制指定数据库。有存活 client 时复用其 home 和队列，无 client 时由 AFX 独立执行；无需本机 seed 历史。默认开启 upload，只上传本轮明确关联的 attempt；`--no-upload` 可关闭，但正在自动上传的共享 session 会被明确拒绝，需先停止该 session。

AFXX request_id/run 保存队列路径、task ID、attempt ID、结果摘要和日志，不复制共享数据库。结果通过 `afx results list <database> --attempt <id>` 精确读取；task→attempt 尚无公开查询接口，仅在适配层只读查询队列表。队列任务由 client 上传，AFXX 标记 upload.owner=client、state=delegated，不再执行整批补传；delegated 不等于服务端已确认。仅独立执行的 attempt 由 AFXX 通过临时数据库调用 `afx sync once`，结束即清理。

由 AFX 管理任务优先级和设备分配；AFXX 不会自动抢占设备或暂停 client。

同一修复流程复用 `request_id`，每次 replay 自动新增一个 `run`。不需要 `--new-run`，也不引用 deploy ID。断线后先查记录，重复调用 replay 会再次提交。

```bash
afxx replay list --request-id repair-F152
afxx replay show --request-id repair-F152          # 默认最新一次
afxx replay show --request-id repair-F152 --run 1
afxx replay compare --request-id repair-F152 --runs 1 2
```

AFXX 通过 AFX 公共结果查询接口按本轮 attempt ID 读取结果，完整检查 seed、CPU/AF-MLIR 后端和 shape 覆盖。终态查看 `state`、`passed` 和结果；CPU 参考允许 `not_checked`，其他后端要求精度通过。队列等待超时不等于执行失败；AFXX 继续按 task ID 等待至配置的期限，缺失或不完整结果不会视为成功。

`replay show` 的 `environment` 来自执行结果；`submission_environment` 是启动端快照。replay.workers 不控制此逐 seed 入口的调度；执行由 AFX 负责。

`compare` 按完整 seed、backend、shape_index 对齐，区分变化、新增、缺失和无法对齐，不会自动把“少了失败记录”判为修复成功。

#### 定位 dump 与结果

`--dump` 或 `replay.dump: true` 传递给 AFX 逐任务开启采集。使用 `evidence list/read --request-id ID --run N` 按需读取远端产物（省略 run 为最新一次，分页固定返回的 run）；不自动复制整个共享 dump 目录。新增或发生文件变化的 worker 目录仅标为候选证据，只有诊断中的明确路径才能关联到 attempt；文件种类及保留期限由 AFX 管理，缺少 IR/kernel 时仍需报告证据缺口。

```bash
afxx replay --request-id repair-F152 --seed <seed> --dump
afxx evidence list --request-id repair-F152 --run 1
afxx evidence read --request-id repair-F152 --run 1 --path <返回的文件路径>
```

读取与分页参数见[状态与证据](docs/afxx-commands.md#8-状态与证据)。

### 平台问题处理

先配置[平台连接](docs/CONFIGURATION.md#平台连接)，本地 agent token 需要相应 issue 权限。

| 阶段 | 常用命令 | 用途 |
|---|---|---|
| 查找问题 | `issue list`、`issue detail`、`issue cases` | 查看问题、生命周期和关联用例 |
| 领取与理解 | `issue claim`、`issue preview` | 保存领取上下文，获取失败摘要和 seed |
| 保持租约 | `issue renew` | 修复期间续租 |
| 本机定位 | `replay`、`replay show/compare` | 在当前 NPU 安装环境中复现和对比 |
| 平台验证 | `issue verify`、`issue verify-check` | 提交候选包，检查验证和关联回归 |
| 查询失败 | `issue tasks`、`execution show` | 查看具体执行日志与失败任务 |
| 收尾 | `issue submit-summary`、`issue release` | 显式发布已检视总结，或退出并释放租约 |

`replay` 是本机发起的复现；`issue verify` 是平台验证。二者分别留存结果，replay 通过不自动完成 issue。

#### 领取并查看问题

```bash
afxx issue list
afxx issue claim --issue-id F152 --request-id claim-F152
afxx issue preview --issue-id F152
# 修复期间按租约到期时间提前续租
afxx issue renew --issue-id F152
```

claim 必须指定 issue_id。preview 默认显示失败摘要和全部去重 seed；需要更多内容时加 `--full`。基础复现、定位和修复过程遵循[修复 skill](skills/afx-issue-repair/SKILL.md)。

#### 多轮部署和复跑

每轮修改使用新的部署 ID，replay 继续使用同一个分组 ID：

```bash
afxx deploy --issue-id F152 --request-id deploy-F152-1
afxx status --request-id deploy-F152-1
# 确认部署成功；有存活 client 用 restart，无存活 client 用 start
afxx client status
afxx client restart
afxx replay --request-id repair-F152 --seed <seed>
afxx replay show --request-id repair-F152
```

需要继续修复时，下一轮部署用 `deploy-F152-2`，replay 仍用 `repair-F152`。关联 issue 的部署会在构建期间续租。基线、候选版本与每次复跑结果应分别保留，不能把“任务已结束”当作问题已修复。

#### 提交 verify 并检查回归

```bash
# 确认最终部署成功；有存活 client 用 restart，无存活 client 用 start
afxx client status
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

### 平台数据查询

查询用例、历史执行、当前平台身份和手动复测批次：

```bash
afxx case list --backend af-mlir --status fail --limit 50
afxx case spec --case-key <seed>
afxx case history --case-key <seed> --limit 50
afxx execution show --exec-uuid <exec_uuid>
afxx platform me
afxx platform summary --scope mine
afxx retest list
```

`case spec` 返回图元数据，`case history` 返回执行历史，具体日志通过 `execution show` 读取。列表返回 next_cursor 时，用 `--after` 继续分页。`platform me` 查询 token 对应的身份，`platform summary` 查询运行概况。

`retest list` 查询手动复测批次，不包含 issue verify 轮次；后者使用 `issue status/tasks` 或 `issue verify-check`。这些查询不要求先领取 issue；可见范围由平台权限决定。完整参数见[平台查询](docs/afxx-commands.md#平台查询与释放租约)，覆盖范围见[API 对照](docs/PLATFORM_API_MAPPING.md)。

### 独立随机测试

需要单独运行有限数量的随机用例时：

```bash
afxx fuzz run --count 100 --request-id fuzz-001
afxx status --request-id fuzz-001
afxx fuzz status --job-id fuzz-001
afxx fuzz stop --job-id fuzz-001
```

这是独立的随机测试能力，使用 `<workflow_root>/fuzz/`，不属于上述 replay 修复主线。省略 count 会持续运行；更新环境或部署前应停止相应任务。status 的 success 表示进程正常结束，不代表全部随机用例通过。详见[命令参考](docs/afxx-commands.md)。

## Agent 接入

### Skill 安装与使用

安装随 AFXX 发布的修复和总结 skill：

```bash
afxx skills install --agent codex
```

安装到 `~/.agents/skills/afx-issue-repair` 和 `afx-issue-summary`。相同内容重复安装会跳过；已有内容不同则返回 `SKILL_CONFLICT`，保留旧文件，需先备份并移走旧目录再安装。

调用示例：`使用 $afx-issue-repair 修复 AFX issue F152，验证和回归通过后 commit，等待检视。` 定位时添加 `--dump`，按[定位 dump 与结果](#定位-dump-与结果)读取实际产物。

### MCP 配置与调用

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

### CLI 与 MCP 的关系

Skill 定义修复流程，CLI 和 MCP 提供执行入口并共用业务实现。优先使用结构化 MCP 工具；没有对应工具的命令可通过 `afxx_run` 调用，也可以直接执行 CLI。具体查询恢复规则见[Agent 调用约定](docs/AGENT_USAGE.md)。

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

## 开发、测试与发布

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
