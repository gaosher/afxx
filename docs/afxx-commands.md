# afxx 命令参考

平台 API 覆盖程度、结构化 MCP 工具及待补接口见[实现对照表](PLATFORM_API_MAPPING.md)。

所有命令以 `afxx` 开头，参数放在子命令之后：

```bash
afxx env init --config /path/config.yaml
```

- 本文的"必填"仅指**命令行参数**。远端地址、源码路径、构建并行度等来自配置文件，见 [配置说明](CONFIGURATION.md)。
- 具体命令的帮助：`afxx <命令> --help`。

## 目录

1. [命令速查](#1-命令速查)
2. [典型流程](#2-典型流程)
3. [通用参数](#3-通用参数)
4. [ID 类型对照](#4-id-类型对照)
5. [配置与诊断](#5-配置与诊断)
6. [环境管理](#6-环境管理)
7. [部署与复跑](#7-部署与复跑)
8. [状态与证据](#8-状态与证据)
9. [平台问题与执行查询](#9-平台问题与执行查询)
10. [dry-run 说明](#10-dry-run-说明)
11. [MCP 调用](#11-mcp-调用)

---

## 1. 命令速查

按一级命令（`afxx` 后的第一个词）分成若干子表。带二级命令的，二级命令紧跟其后，如 `afxx env init`。

### 无二级命令

直接使用，如 `afxx deploy`。

| 命令 | 作用 | 执行方式 |
|---|---|---|
| `help` | 查看顶层命令 | 本地 |
| `doctor` | 检查配置、本地工具、npussh 登录 | 本地检查 |
| `deploy` | 同步源码，构建并安装 AF | 默认跟随日志，可 `--detach` |
| `replay` | 通过 afx fuzz replay 复跑 seed | 异步，每次返回 request_id/run |
| `status` | 查询任务状态 / 单次执行 / 等待变化 | 查询 |

### `config`

| 二级命令 | 作用 | 执行方式 |
|---|---|---|
| `init` | 生成带注释的 YAML 配置 | 本地写文件 |

### `env`

| 二级命令 | 作用 | 执行方式 |
|---|---|---|
| `init` | 准备 Python、CANN、venv 和平台凭据 | 跟随日志，可 `--detach` |
| `update` | 显式更新环境组件或查询版本 | 跟随日志，可 `--detach` |
| `check` | 检查远端环境及 Python 依赖 | 同步返回结果，不创建任务或 ID |

### `evidence`

| 二级命令 | 作用 | 执行方式 |
|---|---|---|
| `list` | 列出 replay 的远端证据文件 | 查询 |
| `read` | 读取单个远端证据文件 | 查询 |

### `issue`

| 二级命令 | 作用 | 执行方式 |
|---|---|---|
| `claim` | 领取平台问题 | 平台写操作 |
| `renew` | 续租已领取的问题 | 平台写操作 |
| `preview` | 查看受影响用例、失败摘要和 seed | 只读 |
| `detail` | 查看生命周期、租约和历史轮次 | 只读 |
| `tasks` | 查询一轮验证 / 回归任务 | 只读 |
| `regression-preview` | 查看平台回归候选集 | 只读 |
| `verify` | 提交已部署 AF 的修复身份 | 连接 NPU 后提交平台请求 |
| `status` | 查询验证和关联回归结果 | 查询 |
| `wait` | 等待验证和关联回归结束 | 阻塞查询；等待期间自动续租 |
| `submit-summary` | 提交人工检视后的修复总结 | 平台写操作 |

### `execution`

| 二级命令 | 作用 | 执行方式 |
|---|---|---|
| `show` | 查看平台执行详情、Shape 和日志 | 只读 |

## 2. 典型流程

> 示意，按需取舍。

```text
首次准备：config init → 编辑配置 → doctor → env init（成功）→ env check（同步结果）
独立复测：deploy → status（成功）→ replay → replay show（终态，获取 evidence_path）；replay 本身不依赖 deploy
平台修复：issue claim → 复现/修改 → deploy → status（成功）
        → issue verify → issue verify-check（验证和关联回归通过）
        → 生成 summary.yaml → 人工检视 → issue submit-summary
```

## 3. 通用参数

| 参数 | 适用命令 | 说明 |
|---|---|---|
| `-h` / `--help` | 所有命令及命令分组 | 显示帮助；不读取配置，不连接远端 |
| `--config PATH` | 除 `help`、`config init` 外的业务命令 | 默认 `~/.config/afxx/config.yaml`。仅使用显式指定或默认路径，**不会**查找工作目录或配置环境变量 |
| `--request-id ID` | `deploy`、`replay`、`status`、`issue claim` | 见下方规则 |
| `--request-key KEY` | `issue release`、`issue submit-summary` | 可选，默认按操作、平台、issue、租约及内容生成稳定去重键 |
| `--dry-run` | `env init/update`、`deploy`、`replay`、`doctor` | 默认关闭；跳过远端连接，见 [第 10 节](#10-dry-run-说明) |
| `--detach` | `env init`、`env update`、`deploy` | 默认关闭（CLI 跟随日志直到结束）。开启后提交即返回，用 `status` 查询。MCP 下自动开启 |
| `issue verify` | `--request-id ID --issue-id ID` | 采集当前安装的包；同组/租约/包复用回执，包变化生成新验证，不选择历史部署 |

**`--request-id` 规则**

- 格式：1–128 位字母、数字、`_`、`.`、`-`。
- deploy/replay 省略时自动生成新分组；查询命令需指定已有 ID。
- 网络重试：复用原 ID。
- 配置或参数发生变化：使用新 ID。

## 4. ID 类型对照

四类 ID 容易混淆，不可互换：

| ID | 示例 / 格式 | 用在哪里 |
|---|---|---|
| 平台问题 ID | `F152` | `issue *` 命令的 `--issue-id` |
| AFXX job ID | 1–128 位字母、数字、下划线、点或连字符（排除 .、..、requests） | fuzz 的 `--job-id` 或旧 verify 兼容入口；deploy/replay 使用 request_id/run |
| AFX attempt ID | AFX 单次执行 ID | `status --attempt` |
| 平台 exec UUID | 平台返回的 exec_uuid，未必为标准 UUID；8–64 位字母、数字、`_`、`-` | `case execution --exec-uuid`（不接受数字 exec_id 或 AFXX job ID） |

`--job-id` 引用哪类 job：

| 命令 | `--job-id` 指向 |
|---|---|
| `replay` | 不引用 job，使用流程 request_id |
| `issue verify` | `--request-id ID --issue-id ID` | 采集当前安装的包；同组/租约/包复用回执，包变化生成新验证，不选择历史部署 |
| `status` | 要查询的任务 |
| `status --attempt` | **replay** job |
| `evidence list/read` | replay 的 request_id/run |

---

## 5. 配置与诊断

### `config init`

生成带注释的 YAML 配置。

| 参数 | 必填 | 说明 |
|---|---|---|
| `--source PATH` | 是 | 本地源码目录 |
| `--output PATH` | 否 | 默认 `~/.config/afxx/config.yaml` |

- 不覆盖已有文件。
- 不接受 `--config`。

### `doctor`

检查配置、本地工具与 npussh 登录。

- 正常模式：查询登录服务。
- `--dry-run`：跳过连接工具及登录检查，仍检查 Git。

## 6. 环境管理

| 命令 | 作用 | 约束 |
|---|---|---|
| `env init` | 准备 Python、CANN、venv 和平台凭据 | 目标 venv 必须不存在。已配置 platform 时，非交互调用需提前准备好本地 token |
| `env update` | 更新显式指定的组件；无参数查询版本 | `--afx VERSION/latest`、`--torch-npu YYYYMMDD/latest`、`--torch VERSION`、`--cann YYYYMMDD/latest`、`--ops`、`--save-config`；afx、torch-npu、cann 省略值默认 latest；torch 必须带确切版本，变更时必须配 torch-npu；ops 必须配 cann |
| `env check` | 检查远端环境及 Python 依赖 | 同步返回检查结果，不创建任务/ID，不支持 --dry-run 或 --detach |

`env init` / `env update` 默认跟随日志，可用 `--detach` 提交即返回。update 读取已有环境清单，未指定组件不更新，默认不改写配置。`--save-config` 在成功后回写实际版本，保留注释；异步任务在后续 status 查询终态时回写，配置被编辑则拒绝覆盖。CANN 默认仅 Toolkit，带 --ops 时使用同批次 OPS。Python 失败尝试回滚；CANN 安装开始后失败需检查现场恢复。详见 [更新规则](CONFIGURATION.md#初始化和更新环境)。

## 7. 部署与复跑

### `deploy`

默认跟随远端日志直到终态，日志写入 stderr，stdout 返回 JSON 结果；部署失败返回对应错误码。`--detach` 提交即返回，MCP 自动启用。`Ctrl+C` 只停止查看，不停止远端任务；连接中断后可通过 `status` 继续查询。

同步源码、构建并安装 AF。异步提交，返回 job ID。

| 参数 | 必填 | 说明 |
|---|---|---|
| `--issue-id ID` | 否 | 关联已领取的问题，并在部署期间自动续租。**不会**提交 verify |

### `replay`

统一调用 `afx fuzz replay`，使用配置的 Python/CANN 和设备参数，默认复用 AFX 队列和结果库，不需要 seed 历史。每次调用都新建 run，返回 request_id/run。无需部署记录或 --new-run。

| 参数 | 必填 | 说明 |
|---|---|---|
| `--request-id ID` | 否 | 流程分组，省略生成新分组；同一 ID 多次提交自动递增 run，不去重 |
| `--seed SEED` | 与 `--seeds-file` 二选一 | 单个 nnsmith seed |
| `--seeds-file PATH` | 与 `--seed` 二选一 | 本地 seed 文件，每行一个；启用批量复跑 |
| `--dump` | 否 | 传入 AFX 逐任务 dump；通过 evidence list/read 读取产物 |

历史查询：

- `replay list --request-id ID`：列出所有 run 及当前状态。
- `replay show --request-id ID [--run N]`：默认最新一次，返回结果和实际环境，终态下载证据。
- `replay compare --request-id ID --runs N M`：仅比较终态，按完整 seed/backend/shape_index 对齐，报告 changed/unchanged/added/missing/unmatched。不会推断“修复成功”。

历史索引在 local.state_dir/replay-groups/；保留该目录才能跨 CLI/MCP 查询相同流程。断线后先查询，重复 replay 会再执行一次。

## 8. 状态与证据

### `status`

三种用法，共用 `--request-id`（不再接受 --job-id/--job）：

| 用法 | 额外参数 | 返回 |
|---|---|---|
| `status --request-id ID` | 无 | 运行概要，查询一次。replay 到终态时自动下载完整证据到 `/tmp`，返回 `evidence_path` 和失败基础信息 |
| `status --request-id ID --attempt AID` | `--attempt`：AFX 执行 ID（仅用于已有内部执行记录；新 replay 使用 replay show）；`--fields A,B`：选择字段，支持 `metadata.graph` 等点路径，仅能与 `--attempt` 搭配 | 单次执行摘要或所选字段。**不触发**全量证据下载 |
| `status --request-id ID --wait` | `--since CURSOR`：上次状态游标；`--timeout SECONDS`：默认 30，范围 (0, 50] | 状态变化、终态或简短超时回执；replay 终态会下载证据 |

约束：

- `--wait` 与 `--attempt` 互斥。
- `--since`、`--timeout` 只能与 `--wait` 一起使用；不传 since 时，以首次查询为基线等待下一次变化，已到终态则直接返回。

补充：

- init 成功后，`status` 查询也会完成凭据同步，用于 detach / MCP 或中断日志跟随后的恢复。
- replay 证据已下载时，优先读取 `evidence_path` 下的本地文件；重复查询终态会复用校验通过的本地缓存。
- 当前**没有** `status --full`、`report download`、`evidence download` 命令。

### `evidence list`

列出 replay 的远端证据文件。

| 参数 | 必填 | 说明 |
|---|---|---|
| `--request-id ID` | 是 | replay 流程 ID |
| `--run N` | 否 | 轮次，省略为最新一次；返回解析后的 run |
| `--offset N` | 否 | 默认 0；非负的文件序号，翻页用返回的 `next_offset` |

返回：request_id、run、远端文件清单、摘录和明确的 attempt 关联。翻页和续读固定返回的 run，避免最新轮次改变。CLI/MCP 不再接收 evidence 的 job_id；不存在的分组/轮次、配置冲突和 dry-run 均不会发起证据读取。

### `evidence read`

读取单个远端证据文件。

| 参数 | 必填 | 说明 |
|---|---|---|
| `--request-id ID` | 是 | replay 流程 ID |
| `--run N` | 否 | 轮次，省略为最新一次；返回解析后的 run |
| `--path PATH` | 是 | 远端证据文件的绝对路径 |
| `--offset N` | 否 | 默认 0，必须非负，单位为字节 |
| `--limit N` | 否 | 默认 8192，范围 1–65536 |
| `--sha256 HASH` | 否 | 校验续读期间文件未发生变化 |

返回：有界文本、哈希和续读位置。不读取任意文件，不读取二进制。

## 9. 平台问题与执行查询

- 所有 `issue *`、`case execution` 命令都支持 `--config`。
- claim/verify 使用 `--request-id`；release/submit-summary 使用可选的 `--request-key`，默认自动生成，旧参数不再接受。
- `issue verify` 连接 NPU 后提交平台请求；其余命令由本地处理，按需访问平台。`issue preview --full` 读取本地领取回执。
- preview、renew、wait、submit-summary 需要已有本地领取记录；verify 需要显式指定已领取问题，或使用已关联问题的 deploy job。detail、tasks、regression-preview、status 和 case execution 不要求先领取。

### 领取与续租

| 命令 | 必填参数 | 可选参数与说明 |
|---|---|---|
| `issue claim` | `--issue-id ID` | 必须明确指定问题。自动生成的 request-id 会在请求前输出，**重试需保留** |
| `issue renew` | `--issue-id ID` | 使用本地保存的领取上下文 |

### 查看信息（只读）

| 命令 | 必填参数 | 可选参数与说明 |
|---|---|---|
| `issue preview` | `--issue-id ID` | 查看受影响用例、失败摘要和 seed。`--full`：返回完整领取复现信息和相关知识。**这不是回归候选集** |
| `issue detail` | `--issue-id ID` | 查看生命周期、租约和历史轮次 |
| `issue tasks` | `--issue-id ID`、`--round-no N` | 查询一轮验证 / 回归任务 |
| `issue regression-preview` | `--issue-id ID` | 查看平台回归候选集；只读，不触发回归 |
| `case execution` | `--exec-uuid UUID` | 查看平台执行详情、Shape 和日志；仅接受平台执行 UUID |

### 验证与回归

| 命令 | 必填参数 | 可选参数与说明 |
|---|---|---|
| `issue verify` | `--request-id ID --issue-id ID` | 采集当前安装的包；同组/租约/包复用回执，包变化生成新验证，不选择历史部署 |
| `issue status` | `--issue-id ID`、`--round-no N` | 查询一次；`round-no` 使用 `verify` 的返回值 |
| `issue wait` | `--issue-id ID`、`--round-no N` | `--timeout SECONDS`：默认 3600，必须为有限正数；**agent 建议传 30**。等待期间自动续租，仅在变化时报告进度；超时返回 PLATFORM_WAIT_TIMEOUT，可继续等待同一轮次 |

验证通过后的回归由平台安排，没有单独的 AFXX regression run 命令。

### 提交总结

| 命令 | 必填参数 | 说明 |
|---|---|---|
| `issue submit-summary` | `--issue-id ID`、`--summary-file PATH`；可选 `--request-key ID` | `--summary-file` 为本地 YAML，建议按 [证据链模板](../skills/afx-issue-summary/assets/summary.yaml) 填写。这是**平台写操作**，不是生成本地总结；修复流程不会自动提交，需人工检视后执行 |

权限、租约及总结格式见 [Agent 调用约定](AGENT_USAGE.md)。

release 与 submit-summary 默认按操作、平台、issue、租约及内容生成稳定 key；相同输入重试复用 key，可用 `--request-key` 覆盖。同一手动 key 对应不同内容时返回 REQUEST_CONFLICT。MCP release 使用 `afxx_issue(action="release", issue_id=..., request_key=...)`；总结使用 `afxx_run(command="issue submit-summary", arguments=[...], request_key=...)`。两者均可省略 request_key，不接受 request_id，也不经过 NPU。

## 10. dry-run 说明

适用命令：`env init/update`、`deploy`、`replay`、`doctor`。

| 命令类型 | dry-run 行为 |
|---|---|
| 任务命令（env / deploy / replay） | 跳过远端连接，仍生成本地配置、脚本和请求记录；`deploy` 还会生成源码快照。本地目录见响应中的 `local` |
| `doctor` | 只做本地检查 |

注意：

- dry-run 成功只代表**本地准备完成**，不证明远端环境可用或编译成功。
- 正式执行时去掉 `--dry-run`，**不能复用** dry-run 的 request-id。
- dry-run 不会输入或同步 token。

## 11. MCP 调用

MCP 与 CLI 共用命令实现。优先使用结构化工具，不需要拼命令参数：

| 工具 | 主要参数 |
|---|---|
| afxx_deploy | request_id、dry_run |
| afxx_replay | request_id、seed / seeds_file（二选一）、dump、upload |
| afxx_job_status | request_id、attempt、fields、wait、since、timeout |
| afxx_client | action=status/start/stop/restart、request_id（变更操作必填）、timeout |
| afxx_issue | action=claim/preview/verify/release、issue_id（必填）；其余参数按 action 校验 |
| afxx_issue_status | issue_id、round_no |
| afxx_operation_status | request_id（本机调用记录，不需要 config） |

业务工具均支持可选的 config。其余操作可用 `afxx_run(command, arguments, config, request_id, request_key)`；arguments 是 CLI 参数数组，不经 shell。`afxx_help` 提供 CLI 帮助。

### 配置冲突

工具 config 和 arguments 中的 `--config PATH` / `--config=PATH` 同时出现时，展开为绝对路径后必须一致；不同则返回 CONFIG_CONFLICT，不执行 CLI。同一路径会去重。显式配置覆盖 MCP 服务 `--config`，服务配置仅作为默认值；都未设置时使用 `~/.config/afxx/config.yaml`。`config init` 使用 --output，拒绝工具 config/--config，不注入服务默认配置。

### 长调用和查询

`deploy`、`env init/update`、`client start/stop/restart`、`issue verify` 返回本机调用记录，除 env 命令自动分配 ID 外，必须提供工具级 request_id。env 命令拒绝用户指定 request_id（包括 arguments 中的 --request-id），也不会将 MCP 调用记录 ID 传入 CLI。CLI 收到原始分组/请求键；arguments 中不同的 --request-id 返回 REQUEST_CONFLICT。client 的键只用于本机调用去重；deploy/verify 使用修复分组并自动分配本机调用键，不改变 CLI 或平台已有的幂等规则。

```text
afxx_client({"action":"restart","request_id":"F152-restart-1","config":"/absolute/task.yaml"})
afxx_operation_status({"request_id":"F152-restart-1"})
# completed 后检查 data.result.ok=true 且 data.result.data.state=running

afxx_issue({"action":"verify","issue_id":"F152",
                   "request_id":"repair-F152","config":"/absolute/task.yaml"})
afxx_operation_status({"request_id":"<返回的本机回执ID>"})
# 从 data.result 取得 CLI 的轮次回执，再查询 afxx_issue_status
```

调用记录返回 data.request_id、operation_id、command、config、state 和 log；pending 表示本地 worker 仍可确认存在，completed 表示 CLI 已退出（包括错误），原始 CLI JSON 在 data.result 中。顶层 ok=true 只表示记录查询成功；必须检查 data.result.ok/error。worker 消失且无结果时返回 unknown，先读日志、查询远端状态，不自动重跑。OPERATION_NOT_FOUND 表示此本机/账号没有该记录。

本机记录位于 `~/.local/state/afxx/mcp-operations/<operation_id>/`：request.json 保存调用参数、配置哈希和 worker PID，result.json 保存 CLI 回执，stderr.log 保存诊断。重复同一 request_id 和输入返回原记录；不同命令、参数、配置内容或工作目录返回 REQUEST_CONFLICT。启动前配置文件内容变化返回 CONFIG_CHANGED，不执行 CLI。

MCP 断开、取消提交响应的等待或服务退出，不会停止已启动的长调用；用预先选定的 request_id 重连查询。同一台机器、同一账号下可恢复，删除记录后不能再恢复查询；无法确认远端状态时不要换键重试。这里不提供通用 cancel：停止 client/fuzz 应调用相应 CLI/MCP 接口。同步只读查询取消会结束本地查询，不代表取消远端任务。

replay 不创建调用回执，直接返回 request_id/run，用 replay show 查询。

completed 不代表 deploy/env 的远端 job 完成：deploy 从 data.result.data.request_id/run 继续查询 deploy show；env 从任务 ID 继续调用 afxx_job_status。verify 的回执也不等于回归通过，从返回轮次继续调用 afxx_issue_status。MCP 仍为 env init/update 注入 --detach；结构化工具不自动串联 restart 和 verify，顺序由 skill 控制。

## MCP 服务

`afxx mcp [--config PATH]` 启动 stdio MCP 服务，由 MCP 客户端管理进程。该命令不输出普通 CLI 的 JSON 响应，不触发 NPU 操作；`afxx mcp --help` 查看参数。

## 安装本地 skills

```bash
afxx skills install --agent codex
```

随 wheel 提供 afx-issue-repair 和 afx-issue-summary，安装到 ~/.agents/skills/。命令独立于工作流配置和远端连接，返回统一 JSON，包含每个 skill 的路径和 reused 状态。相同内容重复安装会跳过；已有内容不同或目标为符号链接时返回 SKILL_CONFLICT，并保留已有文件。更新前先保留并移走存在差异的目录，再执行安装命令。

## Client 生命周期

```bash
afxx client status --config "$TASK_CONFIG"
afxx client start --config "$TASK_CONFIG"
afxx client stop --config "$TASK_CONFIG" --timeout 300
afxx client restart --config "$TASK_CONFIG" --timeout 300
```

四个接口不创建 workflow job，按远端当前用户的存活 client 选择目标。只有一个时使用其实际 home；daemon 与对应 supervisor 算一个 client，两个独立 daemon 即使 home 相同也算冲突。多个存活 client 时包括 status 在内均返回 PLATFORM_CLIENT_CONFLICT。无法识别 home 或发现旧版常驻 client 时返回 PLATFORM_CLIENT_UNTRACKED，不猜测目标、不强杀进程。

没有存活 client 时：status/stop 返回 stopped、daemon_running=false、home=null；restart 返回 PLATFORM_CLIENT_NOT_RUNNING。只有 start 使用 AFX 当前 data_home（AUTOFUSEX_HOME → active-home.json → 系统默认目录）启动。已有目录沿用 AFX 保存的身份、平台地址、设备和凭据，不与 workflow 配置比较。首次创建才使用 platform.client_token_file 等配置。

status 不启动 daemon、不加载 CANN、不读取平台 token；运行会话不等于可以领取复测，retest_readiness 当前固定为 not_checked。stop 等待在途任务退出后关闭 daemon；restart 完成停止后使用当前配置的 Python/CANN 环境启动同一个 home。start 可能开启 fuzz；不删除身份、历史和平台记录。超时返回 PLATFORM_CLIENT_TIMEOUT，先查询状态，不强杀 worker。进程扫描不替代 AFX 的并发启动互斥管理。

--timeout 范围为 (0,3600] 秒，默认 300，控制等待期限；SSH、准备和单次 AFX 请求另有开销。中断本地命令不意味着远端已取消，先查询 status。MCP 使用同名 `client status/start/stop/restart`，参数与 CLI 一致。

平台派发、runner 环境和结果身份问题仍待定位；这些接口只管理生命周期，不保证复测已领取或使用了正确候选包。replay 统一进入 managed 队列。

### 平台查询与释放租约

这些命令在本机使用配置中的平台地址和 `platform.token_file`，不依赖 NPU 登录、远端 AFX 或已领取的 issue（释放除外）。CLI 和 MCP 共用实现，保留统一 JSON 成功/错误返回。

| CLI | 结构化 MCP 工具 | 平台接口 |
| --- | --- | --- |
| `afxx issue release --issue-id F152 [--request-key release-F152-1]` | `afxx_issue(action="release", issue_id, request_key)` | `POST /api/issues/{issue_id}/release` |
| `afxx case spec --case-key <seed>` | `afxx_case(action="spec", case_key=...)` | `GET /api/cases/{case_key}/spec` |
| `afxx case history --case-key <seed> --limit 50` | `afxx_case(action="history", case_key=..., limit=50, after=None)` | `GET /api/cases/{case_key}/history` |
| `afxx case execution --exec-uuid <uuid>` | `afxx_case_execution(exec_uuid=...)` | `GET /api/executions/{exec_uuid}` |
| `afxx retest list` | `afxx_run(command="retest list")` | `GET /api/retests` |

所有入口支持可选配置路径：CLI `--config`，MCP `config`。通用 MCP `afxx_run` 也支持这些命令。

- `issue release` 释放本地领取记录对应的租约，不取消远端 job 或平台复测。平台确认后标记本地租约已释放，后续 preview/renew 不再使用它。网络异常使用同一 `request-key` 重试；本地保留请求与成功回执，不会重复释放。重新领取后默认生成新的 request-key；手动覆盖时需换新 key，旧请求不能释放新租约。平台决定释放后的 issue 状态，不保证所有状态都回到 open。
- `case spec` 返回保存的图元数据（节点、shape variants、weights 等），不是一次执行的结果；`{}` 表示没有保存 spec。不存在的 case 保留平台错误。
- `case history` 返回 `case`、`executions`、`total`、`next_cursor`，按执行 ID 倒序。将 `next_cursor` 作为下一次 `--after`，为 null 时结束。`limit` 为 1–200，默认 50。保留 `is_head` 和 `stale_graph`，避免把旧图执行误认为当前图结果；具体日志使用 `case execution --exec-uuid ...`。
- `retest list` 返回当前用户最近的手动复测批次及其任务，当前平台最多返回 50 个批次且没有公开分页参数。它不包含 issue verify 轮次；verify 使用 `issue status --issue-id ... --round-no ...` 和 `issue tasks` 查询。

### 问题发现与平台身份

```bash
afxx issue list --status open --limit 50
afxx issue list --dimension <dimension> --backend <backend> --q <text> --after <next_cursor>
afxx issue cases --issue-id F152 --role affected
afxx issue cases --issue-id F152 --role regression --after 100
afxx platform me
afxx platform summary --scope mine
```

四个命令均为本机只读 HTTP 查询，使用 `platform.token_file`，无需领取 issue、登录 NPU 或启动 client；支持 `--config`。

- `issue list`：筛选项 `--dimension/--backend/--status/--q` 均可选。`--limit` 默认 50，范围 1–200。返回 `issues` 和 `next_cursor`，将字符串游标原样传入 `--after`；null 表示结束。查询不自动领取问题。
- `issue cases`：必须指定 `--issue-id`；`--role` 为 affected（默认）或 regression。返回 `cases` 和整数 `next_cursor`，下一页传 `--after`。保留 stale、round_no、result 等平台字段；不会自动排除旧用例。平台当前固定每页最多 100 条，未公开 limit，CLI 不接受该参数。空列表不保证 issue 存在，需要时使用 issue detail 核实。
- `platform me`：返回当前 token 的 user_id 和 role；平台要求 developer 或更高权限，鉴权错误原样沿用统一错误码。
- `platform summary`：`--scope mine` 为默认值，可选 all；返回平台原始运行概况，包含 clients、users 等字段。scope 控制运行快照可见性，非“只返回我的客户端”过滤器；实际可见范围由平台身份权限决定。平台在线标记不等价于能够领取某个 verify 任务。

没有新增独立结构化 MCP 工具。已有通用 `afxx_run` 可使用相同命令和参数 token，例如 `command="issue list", arguments=["--status", "open"]`。

### 用例列表

```bash
afxx case list --backend af-mlir --status fail --limit 50
afxx case list --backend af-mlir --status accuracy_fail --arch A2A3
afxx case list --after 12345
afxx case list --case-key <seed>
afxx case list --case-id 12345
afxx case list --q 'backend:af-mlir status:fail'
```

对应 `GET /api/cases`，本机只读 HTTP 查询，无需领取 issue 或连接 NPU。支持 `--config`。返回 `groups`（每组含 case、executions、more_executions）和整数 `next_cursor`；按用例 ID 倒序分页，将游标传给下一次 `--after`，并沿用原筛选条件，null 表示结束。`--limit` 默认 50，范围 1–200。more_executions=true 时用 `case history` 查询完整执行历史；保留执行的 is_head/stale_graph 信息。

`--status` 支持 pass、fail、function_fail、accuracy_fail。status/arch 必须与 backend 一起使用；q 使用平台搜索语法，不能再混用独立 backend/status/arch。case-key/case-id 二选一，精确查询不能混用 q、backend、status、arch 或 after，避免平台静默忽略参数。搜索语法的解析与错误由平台负责。

MCP 仅通过 `afxx_run(command="case list", arguments=[...])` 调用，不新增独立工具。


### 结构化 MCP 定位接口

| MCP 工具 | 必填参数 | 可选参数 | 对应 CLI |
| --- | --- | --- | --- |
| `afxx_case_execution` | `exec_uuid` | `config` | `case execution` |
| `afxx_issue_status` | `issue_id`、`round_no` | `include_tasks=false`、`config` | `issue status [--include-tasks]` |
| `afxx_evidence_list` | `request_id` | `run`、`offset=0`、`config` | `evidence list` |
| `afxx_evidence_read` | `request_id`、`path` | `run`、`offset=0`、`limit=8192`、`sha256`、`config` | `evidence read` |

以上工具共用 CLI 实现并直接返回统一 JSON 结果，不创建长操作回执。前两个只查询平台，不要求领取租约或连接 NPU；后两个通过配置连接远端读取已有 replay 产物，不触发新的执行或 dump。

`exec_uuid` 为 8–64 位字母、数字、下划线或连字符，不是数值 exec_id；`round_no` 为 1–999999。`request_id` 为 1–128 位字母、数字、下划线、点或连字符（排除 .、..、requests）。list 的 offset 是文件列表游标，read 的 offset/limit 是字节偏移和长度，limit 为 1–65536。path 使用 evidence list 返回的远端绝对路径，不展开成本机路径；已有 CLI 继续执行证据目录与符号链接限制。连续读取可携带上次返回的 64 位小写 sha256，文件发生变化则报错。

平台错误、EVIDENCE_UNAVAILABLE、日志缺失/截断和分页信息沿用 CLI 返回，不改写为成功或完整证据。调用示例：

```json
{"request_id":"repair-F152","run":1,"path":"/remote/results/dump/kernel.cpp","offset":0,"limit":8192}
```

`afxx_client` 合并 client 生命周期入口：status 默认 timeout=30，直接返回查询结果，不接受 request_id；start/stop/restart 默认 timeout=300，必须提供 request_id，返回可通过 afxx_operation_status 查询的回执。原四个独立 MCP 工具已移除，CLI 命令保持不变。


`afxx_issue` 将四个动作合并为一个结构化入口，所有动作必填 issue_id：

| action | 其他必填参数 | 可选动作参数 | 返回方式 |
| --- | --- | --- | --- |
| claim | request_id | 无 | 直接返回领取结果 |
| preview | 无 | full | 直接返回失败摘要与 seed |
| verify | request_id | deploy_job_id（旧入口） | 调用回执；先查 operation_status，再查 issue_status |
| release | 无 | request_key | 直接返回释放结果 |

均支持 config。缺少必填参数或传入无关参数直接报错（例如 claim 传 full=false 也不接受）。原四个独立 MCP 入口已移除；issue_status 保留，include_tasks 可附带任务。CLI 与 afxx_run 的原有参数规则不变。verify 不自动 restart client；request_id 用于本地调用回执，平台幂等仍由 CLI 管理。


### MCP 查询收敛

- `afxx_case(action="spec|history", case_key, ...)` 统一 case 查询。history 支持 after/limit（默认 50）；spec 拒绝分页参数。原 case_spec/history 独立工具已移除。
- `afxx_issue_status(issue_id, round_no, include_tasks=false)` 默认只返回验证/关联回归摘要；true 时分别在 verification.tasks、regression.tasks 添加各轮任务，无回归时 regression 为 null。round_no 指验证轮次；任一查询失败整体返回错误，不能把摘要当作任务读取成功。CLI 同步支持 `issue status --include-tasks`。原独立 issue_tasks 工具已移除，CLI issue tasks 仍可查询任意轮次。
- `afxx_job_status` 支持 attempt 和 fields（字段名数组），转发 CLI --attempt/--fields；fields 需要 attempt，attempt 与 wait 互斥，since/timeout 仅在 wait=true 时有效（默认 30 秒）。不接受无关参数或空字段列表。
- 手动复测列表使用 `afxx_run(command="retest list")`，不用于查询 issue verify；原独立 retest_list 工具已移除。
- 优先使用结构化工具。仅在没有对应工具时使用 afxx_run，不熟悉参数先调用 afxx_help。短调用直接返回 CLI envelope；长调用（env init/update、deploy、client start/stop/restart、issue verify）除 env 命令自动分配外要求 request_id，先返回调用回执，再查 afxx_operation_status；CLI 完成后仍须按 job_id/round_no 跟踪远端执行。

当前注册 13 个 MCP 工具（10 个业务工具、3 个辅助工具），afxx_run 仍支持 38 条 CLI 命令。


### issue verify-check

`afxx issue verify-check --issue-id F152 --round-no 1 [--wait --timeout 30]`，MCP 使用 `afxx_issue(action="verify-check", issue_id="F152", round_no=1, wait=true, timeout=30)`。round_no 是验证轮次，必填；等待默认 30 秒，timeout 必须配 wait，范围 (0,50]。单次 HTTP 请求仍可能占用额外网络超时时间。

只读查询平台详情、验证与关联回归任务，核对包版本/哈希；不读取本地领取记录、不续租、不重启、不提交任务。返回 state、phase、verification/regression（含任务数及分类 counts）、failures、next_action、timed_out。最多展示 20 条失败/异常摘要、每条 detail 最多 1000 字符，同时返回完整数量和截断标记。轮次接口当前不提供 execution UUID；仅在响应中实际存在时保留，不猜测关联。

state=running 表示继续等待；passed 表示任务全部明确通过，且回归已通过或当前最新轮次的 issue 已进入 summarizing/resolved 且无回归候选。没有回归但仍在 verifying/regressing 时继续等待；旧轮次不能借用新轮次的生命周期断言无回归。failed/invalid 保留平台结论；skip、取消、空任务或无法确认生命周期返回 attention，不视为通过。未关联回归时 reason=no_regression_candidates，不宣称回归用例全部通过。

超时直接返回当前进度、timed_out=true、next_action=wait，查询成功仍为 ok=true；平台任务失败也通过 data.state 表达，HTTP/协议错误才返回 ok=false。多个查询不是平台原子快照，遇到不一致应复查；失败列表不读取完整日志。旧 issue status/tasks/detail/wait 保留，旧 wait 仍有自动续租行为，修复 skill 改用只读 verify-check。

deploy 和 replay 使用 request_id 分组，各自从 run=1 计数，每次调用新增一轮；远端目录为 `workflow-state/<request_id>/deploy/run-N/` 和 `workflow-state/<request_id>/replay/run-N/`。本地锁分配轮次，内部执行 ID 保持唯一，提交仍有幂等保护。旧记录不搬迁，查询沿用已保存的路径。修改代码后继续使用同一分组。断线先查询 `deploy list/show` 或 `replay list/show`，不要盲目重提。

`afxx deploy list --request-id repair-F001` 列出部署记录；`afxx deploy show --request-id repair-F001 [--run N]` 查询最新或指定部署。历史记录不选择运行包。verify 使用 `--request-id repair-F001 --issue-id F001` 读取当前安装身份；同组/租约/包的重试复用冻结请求和回执，包变化后生成新验证。

MCP deploy/verify 的本机 operation ID 自动生成，与修复分组不同；使用返回的回执 ID 查询 operation_status，CLI 结果中的 request_id/run 用于分组查询。旧 verify --job-id/deploy_job_id 入口保留为记录兼容入口，也不要求当前包等于历史部署包；新流程使用 request_id。

### deploy 客户端协调

`afxx deploy --coordinate-client` 在编包及包校验后、安装前停止唯一目标 AFX 客户端，安装验证成功后恢复原运行意图；MCP `afxx_deploy` 使用 `coordinate_client=true`。省略参数不协调。无需单独 stop/start，远端 worker 不依赖本地日志连接持续在线。

支持 running（重新 start 会话）、idle（只启动 daemon）、stopped（不启动）。不保证原 run/队列进度续跑；paused、过渡状态或活动管理 operation 在停止前拒绝。注册执行环境的 CANN 必须属于部署目标，恢复后使用注册的 Python/env 核对 AF 库路径和哈希。安装前失败尝试恢复旧会话，安装开始后失败保持停止；恢复失败保留已验证安装并返回 DEPLOY_CLIENT_RESTORE_FAILED，不能继续 verify。status 返回 client_coordination 和 installation_state。

生命周期锁仅约束 AFXX 调用，不是 AFX 的全局接单屏障；协调期间其他入口不得启动任务。原本运行中的平台验证应先结束。停止/恢复失败后查同一 job 和 client status，不绕过占用检查、不强杀、不为恢复而盲目重新安装。

环境操作 `env init/update` 分别自动生成 `ENV-INIT-` / `ENV-UPDATE-` 加 12 位随机十六进制后缀的任务 ID，不接受 `--request-id`。通过返回 ID 使用 `status --request-id ID` 查询。MCP 调用记录与远端环境任务分别自动分配 ID：先用调用回执查询 `afxx_operation_status`，再用其中 CLI 结果的任务 ID 查询远端状态。

`env check` 在 CLI/MCP 均同步返回检查结果，不生成任务或 MCP 调用记录；不支持 request_id、--dry-run、--detach。远端仅临时解包检查代码并在结束后清理，检查失败返回 ENV_CHECK_FAILED 和检查详情，连接中断可直接重跑。
