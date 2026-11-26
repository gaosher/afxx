# AFXX — AFX Extensions

## 简介

AFXX 是面向本地 agent 的 AFX 开发与问题修复工具包，主要功能包括：

1. 准备 afx 运行环境 (cann, torch & torch_npu, afx)
2. 同步本地 graph-autofusion 源码，编包以及安装
3. 提供一套 skill，完成 afx 平台 issue 的 领取 -> 修复 -> 验证 -> 总结 闭环

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

> `config init` 不覆盖已有文件。

### 2. 初始化环境

```bash
afxx env init
```

命令自动上传配置和执行工具，准备远端 Python、CANN 和 AFX 环境，并实时显示日志。等待返回 `ok=true` 且 `state=success` 后再继续；无需手动同步配置。检查、更新及断线恢复见[环境管理](#环境管理)。

### 3. 使用修复 Skill

目前 Skill 安装/升级和 MCP 自动配置仅支持 Codex（`--agent codex`）。其他 agent 可通过支持 stdio 的 MCP 客户端手动接入 `afxx mcp`，或直接使用 AFXX CLI；Skill 的安装目录、加载方式和指令兼容性尚未适配验证，不支持通过 `--agent` 一键安装到其他 agent。

先在本机安装 Skill：

```bash
afxx skills install --agent codex
```

如需通过 MCP 调用结构化工具，在本机安装 MCP 依赖并写入 Codex 配置：

```bash
# 使用安装 afxx 的同一个 Python 环境
python3 -m pip install 'afxx[mcp]'
afxx mcp upgrade --agent codex
```

`mcp upgrade` 支持首次配置，也会更新已有配置以使用当前 AFXX 的 Python 环境；完成后重连 AFXX MCP 或重启 Codex，无需在 NPU 上安装 MCP 服务。

然后在 Codex 对话中输入 Prompt：

```text
使用 $afx-issue-repair 修复 <issue ID>，完成验证后 commit 修复代码并生成 summary.yaml
```

Skill 会完成“领取 → 基线部署与复现 → 定位修复 → 部署验证 → 关联回归”，交付修复 commit、验证结论和 `summary.yaml`，不会自动 push 或发布总结。无需先手动 deploy。

Skill 可以直接使用 AFXX CLI，**不依赖 MCP**；已接入 MCP 时优先使用结构化工具。安装与接入细节见[Agent 接入](#agent-接入)。

## 功能介绍

- [环境管理](#环境管理)
- [源码部署](#源码部署)
- [客户端管理](#afx客户端管理)
- [用例复跑与定位](#用例复跑与定位)
- [平台问题处理](#平台问题处理)
- [平台数据查询](#平台数据查询)

CLI 保持结构化 JSON：交互终端自动缩进换行，管道、重定向及 MCP 保持紧凑输出。日志和进度走 stderr，结果 JSON 走 stdout；stderr 有内容不代表失败，应结合退出码、`ok` 和任务终态判断。

### 环境管理

#### 1. 创建本地配置

```bash
# 创建配置文件，默认位置 ~/.config/afxx/config.yaml
afxx config init --source /absolute/path/graph-autofusion

# 检查配置、本地依赖和登录状态。
afxx doctor
```

> `config init` 不覆盖已有配置，所有命令默认读取 `~/.config/afxx/config.yaml`，使用其他配置时需要追加 `--config /absolute/path/config.yaml`。

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
afxx deploy [--coordinate-client] [--request-id repair-F001]

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

> 默认实时跟随远端日志，完成后返回最终结果。`Ctrl+C` 只停止查看，不终止远端部署，可继续用 `deploy show` 查询。后台提交使用 `afxx deploy --detach`；MCP 自动使用后台提交。

同一修复流程手动指定相同的 `request_id`，每次 deploy 自动新增部署轮次；省略时自动创建新分组。断线后先查询，不要重复提交。

```bash
afxx deploy list --request-id repair-F001
afxx deploy show --request-id repair-F001          # 最新部署
afxx deploy show --request-id repair-F001 --run 1  # 历史部署
```

部署需要暂停并恢复目标 AFX 客户端时使用 `--coordinate-client`，详见[部署客户端协调](docs/afxx-commands.md#deploy-客户端协调)。原来停止的客户端保持停止，运行会话重新启动，不保证原队列续跑。 同一 Python 环境下允许切换 CANN 路径，运行会话恢复后核对目标 AF 库；idle 只恢复 daemon，执行环境待下次启动 session 后刷新。

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

#### 用例复跑

replay 在远端当前安装的环境中复跑 seed。有 client 时复用其队列，没有时临时启动执行进程。

```bash
# 单个 seed 复跑
afxx replay --request-id repair-F001 --seed <seed>

# 批量复跑：文件每行一个 seed
afxx replay --request-id repair-F001 --seeds-file /absolute/path/seeds.txt
```

replay 会校验当前环境中的 afx client 唯一性，多个存活 client 时直接报错。进入 client 队列的结果由 client 上传；独立执行的结果由 AFXX 调用 `afx sync once`。复跑结果统一上传；现有会话关闭上传时，先启用会话上传再复跑。

多轮复跑需要归为同一组时，每次显式传入相同的 --request-id，工具自动新增 run 序号。省略该参数则每次创建新分组。断线后先查询记录，重复执行 replay 会重新提交用例。

#### 复跑结果展示

```bash
# 列出本组所有复跑记录
# 比如：run-1、run-2、run-3
afxx replay list --request-id repair-F001

# 查看最新一轮
afxx replay show --request-id repair-F001
# 指定 --run 可查看历史轮次
afxx replay show --request-id repair-F001 --run 2

# 对比两轮结果
afxx replay compare --request-id repair-F001 --runs 1 2
```

复跑结果分为两部分：`batch.complete` 表示本批是否取得全部用例的终态，`counts` 分别统计 pass/fail/skip。skip 不算通过；只有批次完整且全部 pass，`passed` 才为 true。

- 单例精度、编译、执行失败或明确取消记 fail，`no_autofuse_coverage` 记 skip，均继续后续 seed。
- 已有任务 ID 的等待超时记 pending；单例结果查询失败记 result_unknown。继续后续 seed，保留任务/attempt ID，批次标为不完整，不自动重复提交。
- 未取得回执时停止提交，标记 submission_unknown；队列、鉴权或协议整体异常也停止。`batch.unsubmitted` 列出尚未提交的 seed。
- 上传状态单独记录，不改变用例执行结论。pending/未知结果是本次收集时的状态，不代表远端任务已停止。

`repair-F001` 的远端目录结构示意：

```text
repair-F001/
├── deploy/
│   ├── run-1/               基线部署
│   │   └── packages/          本次 AF run 包
│   └── run-2/               修改代码后的候选部署，文件结构同上
└── replay/
    ├── run-1/                 基线部署后复跑
    │   └── result.json          本轮用例结果、错误诊断和证据索引
    └── run-2/                 更新包后再次复跑
```

- 同一修复流程中，调用 deploy 和 replay 时需手动传入相同的 `--request-id` 才会被归入同一目录下，例如 `repair-F001`；工具不会自动关联。
- 该 ID 下，deploy 和 replay 各自递增 `run`，不要求一一对应。
- replay 和 verify 使用当前安装的包；历史部署用于追溯源码、包和日志。

远端文件位置见[远端目录结构](#远端目录结构)，完整查询参数见[命令参考](docs/afxx-commands.md#7-部署与复跑)。

#### 定位 dump 与结果

```bash
# 开启 dump 复跑
afxx replay --request-id repair-F001 --seed <seed> --dump

# 列出某一轮的 evidence 文件
afxx evidence list --request-id repair-F001 --run n

# 读取其中一个文件的内容，支持通过 --offset --limit 分页读取
afxx evidence read --request-id repair-F001 --run 1 --path <返回的文件路径>
```

读取与分页参数见[状态与证据](docs/afxx-commands.md#8-状态与证据)。

### 平台问题处理

先配置[平台连接](docs/CONFIGURATION.md#平台连接)，本地 agent token 需要相应 issue 权限。

#### 1. 领取问题

```bash
afxx issue claim --issue-id F001 --request-id claim-F001
```

claim 必须指定 `issue_id`，领取后保存问题上下文和租约。

#### 2. 理解问题

```bash
# 获取失败摘要和全部去重 seed
afxx issue preview --issue-id F001
# 需要完整内容时加 --full
afxx issue preview --issue-id F001 --full

# 查看问题详情
afxx issue detail --issue-id F001
# 查看关联用例
afxx issue cases --issue-id F001
```

需要单个用例的图信息和执行历史时，使用下文的[平台数据查询](#平台数据查询)。

#### 3. 定位和修复

通过[源码部署](#源码部署)、[用例复跑与定位](#用例复跑与定位)和[evidence 读取](#定位-dump-与结果)完成“部署基线 → 复现 → 定位 → 修改 → 再部署、复跑”。同一修复流程手动使用相同的 `--request-id repair-F001`。
完整修复流程可参考[修复 skill](skills/afx-issue-repair/SKILL.md)。

#### 4. 平台验证与结果查询

确认最终部署成功后，按[客户端管理](#afx客户端管理)刷新执行进程。确认成功后提交验证。

```bash
# 提交平台验证，使用当前 cann 目录中安装的包
afxx issue verify --issue-id F001 --request-id repair-F001
```

`issue verify` 的请求链路：

```text
本机 AFXX
  → NPU：采集当前安装的 AF 包身份
  → 本机向平台提交 verify（远端安装锁保持至提交结束）
  → 平台创建验证任务
  → 符合条件的 client 领取并执行，上报结果
```

重复提交同一包的验证会返回已有回执；更新包后会创建新的验证轮次。查询超时继续查同一 `round_no`，无需重新提交。

`issue verify-check` 由本机直接查询平台服务器：

```bash
# 使用 verify 返回的 round_no 查询验证及关联回归
afxx issue verify-check --issue-id F001 --round-no <round_no>

# --wait 参数控制等待并轮询，直到结束或超时
afxx issue verify-check --issue-id F001 --round-no <round_no> --wait --timeout 30
```

#### 5. 获取失败结果

```bash
# 查看对应轮次的任务，从失败任务中取得 exec_uuid
afxx issue tasks --issue-id F001 --round-no <round_no>

# 获取具体执行结果和日志
afxx case execution --exec-uuid <exec_uuid>
```

验证或关联回归失败时，使用对应的轮次查询失败任务，再回到定位和修复阶段。

#### 6. 修复总结

使用[总结 skill](skills/afx-issue-summary/SKILL.md)生成本地 `summary.yaml`，格式见[模板](skills/afx-issue-summary/assets/summary.yaml)。检视后可显式发布：

```bash
afxx issue submit-summary --issue-id F001 \
  --summary-file /absolute/path/summary.yaml [--request-key summary-F001]
```

submit-summary 和 release 均默认按操作、平台、issue、租约及请求内容生成稳定去重 key；可用 `--request-key` 手动覆盖。

发布需要满足平台状态、租约和权限要求，不会由 verify 自动触发。

#### 其他辅助操作

```bash
# 查找待处理问题
afxx issue list

# 修复期间按租约到期时间提前续租
afxx issue renew --issue-id F001

# 不再处理时释放租约
afxx issue release --issue-id F001 [--request-key release-F001]
```

完整参数见[命令参考](docs/afxx-commands.md)，平台 API 覆盖范围见[实现对照](docs/PLATFORM_API_MAPPING.md)。

### 平台数据查询

这些命令由本机直接查询平台，不要求先领取 issue；可见范围由平台权限决定。

#### 用例与执行记录

```bash
# 筛选失败用例
afxx case list --backend af-mlir --status fail --limit 50

# 查看图元数据
afxx case spec --case-key <seed>

# 查看用例的执行历史
afxx case history --case-key <seed> --limit 50

# 查看某次执行的结果和日志
afxx case execution --exec-uuid <exec_uuid>
```

列表返回 `next_cursor` 时，用 `--after` 继续分页。

#### 平台身份与运行概况

```bash
# 查询 token 对应的用户身份
afxx platform me

# 查看自己的运行概况
afxx platform summary --scope mine
```

#### 手动复测批次

```bash
afxx retest list
```

查询当前用户在平台页面手动发起的复测批次及任务状态

完整参数见[平台查询](docs/afxx-commands.md#平台查询与释放租约)，覆盖范围见[API 对照](docs/PLATFORM_API_MAPPING.md)。

## Agent 接入

### Skill 安装与使用

安装随 AFXX 发布的修复和总结 skill：

```bash
afxx skills install --agent codex
```

安装到 `~/.agents/skills/afx-issue-repair` 和 `afx-issue-summary`。

- 相同内容重复安装会跳过；
- 已有内容不同则返回 `SKILL_CONFLICT`，保留旧文件，需先备份并移走旧目录再安装。

Skill 调用示例：`使用 $afx-issue-repair 修复 AFX issue F001，验证和回归通过后 commit 修复代码并生成 summary.yaml ，等待检视。`

### MCP 配置与调用

Skill 定义修复流程，CLI 和 MCP 共用执行逻辑。**MCP 可选**：未接入 MCP 时，agent 也可以通过 CLI 使用 skill。

#### 接入 MCP

在本机安装：

```bash
python3 -m pip install 'afxx[mcp]'
```

在支持 `mcpServers` 格式的客户端中添加：

```json
{
  "mcpServers": {
    "afxx": {
      "command": "/absolute/path/to/afxx",
      "args": [
        "mcp"
      ]
    }
  }
}
```

将 `command` 替换为本机 `afxx` 的绝对路径。

默认读取 AFXX 配置 `~/.config/afxx/config.yaml`；指定其他配置时，将 `args` 改为 `["mcp", "--config", "/absolute/path/config.yaml"]`。
客户端运行环境需能找到 `npussh` 等本地工具。

#### 调用与结果查询

Agent 可使用以下结构化工具：

| 工具 | 功能 |
| --- | --- |
| `afxx_deploy` | 同步源码、编包并部署，新增部署轮次 |
| `afxx_replay` | 复跑指定 seed 或 seed 文件，支持 dump 和上传 |
| `afxx_job_status` | 查询远端任务状态，可等待变化或查看指定 attempt |
| `afxx_client` | 查询、启动、停止或重启远端 client |
| `afxx_issue` | 领取、预览、释放 issue，提交验证或检查验证结果 |
| `afxx_issue_status` | 查询验证轮次与关联回归，可附带任务详情 |
| `afxx_case` | 查看用例图结构或执行历史 |
| `afxx_case_execution` | 按 exec_uuid 查看某次执行的结果和日志 |
| `afxx_evidence_list` | 按 request_id/run 列出远端复跑产物 |
| `afxx_evidence_read` | 分页读取远端日志、IR、kernel 等产物 |
| `afxx_operation_status` | 查询 MCP 长操作的本机调用回执及结果 |

另有两个通用工具：

- `afxx_help` 查看命令帮助；
- `afxx_run` 调用尚未提供独立工具的 CLI 命令。

完整参数见[MCP 调用说明](docs/afxx-commands.md#11-mcp-调用)，查询和恢复规则见[Agent 调用约定](docs/AGENT_USAGE.md)。

## 任务记录与清理

### 远端目录结构

默认按 `workflow_root` 推导目录；配置覆盖路径时，以实际配置为准。目录按需创建，并非初始化后全部存在。

```text
<workflow_root>/
├── python/                    workflow 专用 Python
├── venv/                      torch、torch_npu、AFX 等 Python 包
├── Ascend/cann/                CANN 和 deploy 安装的 AF 库
├── graph-autofusion/           同步的源码
│   └── build/                 复用的编译目录
├── auth/                      平台凭据
├── workflow-env-state/        当前环境清单、依赖锁及包缓存
├── workflow-state/
│   ├── ENV-INIT-<后缀>/       环境初始化记录
│   ├── ENV-UPDATE-<后缀>/     环境更新记录
│   └── repair-F001/
│       ├── deploy/run-1/     配置、源码记录、日志、结果及 packages/
│       └── replay/run-1/     配置、日志和本轮结果
└── workflow-results/<执行ID>/ replay 结果与证据归档目录
```

任务目录仅展示常用文件，实际内容随操作类型变化。`env check` 不创建任务目录。deploy/replay 的远端目录与 request_id、类型和 run 对应。原有执行记录不搬迁，仍可按原索引查询。

AFX client 数据目录由 AFX 自己管理，不随 workflow_root 改变；实际 home 可用 `afxx client status` 查看，常见默认布局为：

```text
~/.local/share/autofusex/
├── client.json                client 配置与身份
├── client.log                 客户端日志
├── supervisor/                守护进程相关文件
└── runs/<AFX-run-id>/          AFX 执行会话
    ├── tasks.sqlite           任务队列
    ├── results.sqlite         执行结果
    └── debug/                 诊断产物（开启 dump 后按需生成）
```

dump 也可能位于 client home 的 `debug/` 下；以 `evidence list` 返回的路径为准，不需要手动查数据库或猜目录。

### 记录与清理规则

| 位置 | 内容 |
| --- | --- |
| 本地 `local.state_dir/requests/` | 部署等提交的幂等记录 |
| 本地 `local.state_dir/deploy-groups/`、`replay-groups/` | 本地加锁分配轮次及历史索引 |
| 本地 `local.state_dir/issues/` | issue 领取记录及修复材料 |
| 远端 `workflow-state/<request_id>/{deploy,replay}/run-N/` | 冻结配置、请求、状态、环境、日志、报告及本轮 AF run 包 |
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

[publish.yml](.github/workflows/publish.yml) 在推送 `v*` tag 时执行测试、构建、安装验证并发布 PyPI。

完整接口见[命令参考](docs/afxx-commands.md)、[CLI/MCP/API 对照](docs/PLATFORM_API_MAPPING.md)和[Agent 调用约定](docs/AGENT_USAGE.md)。本项目采用 [Apache License 2.0](LICENSE)。

### 更新本机 skill / MCP

升级 AFXX 包后，执行以下命令同步当前包自带的 skill，并让 Codex MCP 使用当前 AFXX 所在的 Python 环境：

```bash
afxx skills upgrade --agent codex
afxx mcp upgrade --agent codex
```

旧内容自动备份，路径显示在返回结果中。命令不会升级 AFXX 包或重启现有 MCP；完成后重连 MCP，使新版本生效。

`deploy show` 默认返回精简状态、日志路径、包路径/哈希、客户端协调状态及环境告警摘要，不展开依赖明细和完整环境快照。需要完整记录时使用 `afxx deploy show --request-id <ID> --run <N> --full`；MCP 的 `afxx_run(command="deploy show", arguments=[...])` 使用相同参数与返回规则。原始任务记录不裁剪。

`evidence list` 仅返回文件元数据和关联信息，不返回内容摘录；每页最多 32 个文件，使用返回的 `next_offset` 作为下一次的 `--offset`，并固定 `--run`。需要日志、IR 或 kernel 内容时使用 `evidence read --path <返回路径>`。

部署与复跑的日常进度查询可用 `afxx status --request-id <ID> --type deploy|replay --run <N>`；省略 run 查询该类型最新轮次，可加 `--wait --timeout 30` 等待变化。此分组查询只返回状态摘要，不下载证据；详细结果仍使用对应的 show 命令。
