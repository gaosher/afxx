# afxx 命令参考

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
| `deploy` | 同步源码，构建并安装 AF | 异步，返回 job ID |
| `replay` | 用已部署的 AF 复跑 seed | 异步，返回新的 replay job ID |
| `status` | 查询任务状态 / 单次执行 / 等待变化 | 查询 |

### `fuzz`

| 二级命令 | 作用 | 参数 |
|---|---|---|
| `run` | 远端调用官方 AFX 随机 fuzz，异步返回 job ID | `--count` 可选（省略持续运行）；`--device`、`--backend` 可重复；`--workers`、`--compile-threads`、`--ops`、`--config`、`--request-id`、`--dry-run` |
| `status` | 查询官方 AFX 队列和 campaign | 必填 `--job-id`，可选 `--config` |
| `stop` | 请求官方 AFX 停止该 job | 必填 `--job-id`，可选 `--config` |

所有 fuzz job 共用 `<workflow_root>/fuzz/`，自动保存结果。无需指定或创建 SQLite。AFXX 任务状态使用 `afxx status --job-id ...`；进程正常结束不等于全部 fuzz 用例通过。MCP 同步支持 `fuzz run/status/stop`。

### `config`

| 二级命令 | 作用 | 执行方式 |
|---|---|---|
| `init` | 生成带注释的 YAML 配置 | 本地写文件 |

### `env`

| 二级命令 | 作用 | 执行方式 |
|---|---|---|
| `init` | 准备 Python、CANN、venv 和平台凭据 | 跟随日志，可 `--detach` |
| `update` | 更新 torch_npu / AFX | 跟随日志，可 `--detach` |
| `check` | 检查远端环境及 Python 依赖 | 异步，返回 job ID |

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
| `summarize` | 提交人工检视后的修复总结 | 平台写操作 |

### `execution`

| 二级命令 | 作用 | 执行方式 |
|---|---|---|
| `show` | 查看平台执行详情、Shape 和日志 | 只读 |

## 2. 典型流程

> 示意，按需取舍。

```text
首次准备：config init → 编辑配置 → doctor → env init → env check → status
独立复测：deploy → status（成功）→ replay → status（终态，获取 evidence_path）
平台修复：issue claim → 复现/修改 → deploy → status（成功）
        → issue verify → issue wait（验证和关联回归通过）
        → 生成 summary.yaml → 人工检视 → issue summarize
```

## 3. 通用参数

| 参数 | 适用命令 | 说明 |
|---|---|---|
| `-h` / `--help` | 所有命令及命令分组 | 显示帮助；不读取配置，不连接远端 |
| `--config PATH` | 除 `help`、`config init` 外的业务命令 | 默认 `~/.config/afxx/config.yaml`。仅使用显式指定或默认路径，**不会**查找工作目录或配置环境变量 |
| `--request-id ID` | `env init/update/check`、`deploy`、`replay`、`issue claim`、`issue summarize` | 见下方规则 |
| `--dry-run` | `env init/update/check`、`deploy`、`replay`、`doctor` | 默认关闭；跳过远端连接，见 [第 10 节](#10-dry-run-说明) |
| `--detach` | `env init`、`env update` | 默认关闭（CLI 跟随日志直到结束）。开启后提交即返回，用 `status` 查询。MCP 下自动开启 |
| `--job-id ID`（别名 `--job`） | `replay`、`status`、`issue verify`、`evidence list/read` | AFXX 的 32 位小写十六进制任务 ID。各命令引用的 job 类型见各节 |

**`--request-id` 规则**

- 格式：1–128 位字母、数字、`_`、`.`、`-`。
- 省略时自动生成；**`issue summarize` 必须显式提供**。
- 网络重试：复用原 ID。
- 配置或参数发生变化：使用新 ID。

## 4. ID 类型对照

四类 ID 容易混淆，不可互换：

| ID | 示例 / 格式 | 用在哪里 |
|---|---|---|
| 平台问题 ID | `F152` | `issue *` 命令的 `--issue-id` |
| AFXX job ID | 32 位小写十六进制 | `--job-id`：deploy job 或 replay job，因命令而异 |
| AFX attempt ID | AFX 单次执行 ID | `status --attempt` |
| 平台 exec UUID | 平台返回的 exec_uuid，未必为标准 UUID；8–64 位字母、数字、`_`、`-` | `execution show --exec-uuid`（不接受数字 exec_id 或 AFXX job ID） |

`--job-id` 引用哪类 job：

| 命令 | `--job-id` 指向 |
|---|---|
| `replay` | 成功的 **deploy** job |
| `issue verify` | **deploy** job |
| `status` | 要查询的任务 |
| `status --attempt`、`evidence list/read` | **replay** job |

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
| `env update` | 按配置更新 torch_npu / AFX | 要求已有受管理环境。不升级 CANN，修改 `cann.date` 不会触发安装 |
| `env check` | 检查远端环境及 Python 依赖 | 无专用参数；异步提交，返回 job ID |

`env init` / `env update` 默认跟随日志，可用 `--detach` 提交即返回。

## 7. 部署与复跑

### `deploy`

同步源码、构建并安装 AF。异步提交，返回 job ID。

| 参数 | 必填 | 说明 |
|---|---|---|
| `--issue-id ID` | 否 | 关联已领取的问题，并在部署期间自动续租。**不会**提交 verify |

### `replay`

使用已部署的 AF 复跑 seed。异步提交，返回**新的** replay job ID。

| 参数 | 必填 | 说明 |
|---|---|---|
| `--job-id ID` | 与 `--deployment` 二选一 | 引用成功的 deploy job（不是本次 replay job） |
| `--deployment PATH` | 与 `--job-id` 二选一 | 远端 `result.json` 的绝对路径 |
| `--seed SEED` | 与 `--seeds-file` 二选一 | 单个 nnsmith seed |
| `--seeds-file PATH` | 与 `--seed` 二选一 | 本地 seed 文件，每行一个；启用批量复跑 |
| `--dump` | 否 | 开启本次任务的定位 dump，写入冻结配置，不修改用户配置文件。省略时沿用 `replay.dump`（默认 false） |
| `--upload` / `--no-upload` | 否 | 互斥；覆盖本次结果上传设置。省略时沿用 `upload.enabled`；上传使用 `platform.url` 与远端 `platform.client_token_file`（默认复用 `platform.remote_token_file`），调用官方 `platform client once` |

## 8. 状态与证据

### `status`

三种用法，共用 `--job-id`：

| 用法 | 额外参数 | 返回 |
|---|---|---|
| `status --job-id ID` | 无 | 运行概要，查询一次。replay 到终态时自动下载完整证据到 `/tmp`，返回 `evidence_path` 和失败基础信息 |
| `status --job-id ID --attempt AID` | `--attempt`：AFX 执行 ID（`--job-id` 须为 replay job）；`--fields A,B`：选择字段，支持 `metadata.graph` 等点路径，仅能与 `--attempt` 搭配 | 单次执行摘要或所选字段。**不触发**全量证据下载 |
| `status --job-id ID --wait` | `--since CURSOR`：上次状态游标；`--timeout SECONDS`：默认 30，范围 (0, 50] | 状态变化、终态或简短超时回执；replay 终态会下载证据 |

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
| `--job-id ID` | 是 | replay job |
| `--offset N` | 否 | 默认 0；非负的文件序号，翻页用返回的 `next_offset` |

返回：远端文件清单、摘录和明确的 attempt 关联。

### `evidence read`

读取单个远端证据文件。

| 参数 | 必填 | 说明 |
|---|---|---|
| `--job-id ID` | 是 | replay job |
| `--path PATH` | 是 | 远端证据文件的绝对路径 |
| `--offset N` | 否 | 默认 0，必须非负，单位为字节 |
| `--limit N` | 否 | 默认 8192，范围 1–65536 |
| `--sha256 HASH` | 否 | 校验续读期间文件未发生变化 |

返回：有界文本、哈希和续读位置。不读取任意文件，不读取二进制。

## 9. 平台问题与执行查询

- 所有 `issue *`、`execution show` 命令都支持 `--config`。
- 只有 `issue claim` 和 `issue summarize` 支持 `--request-id`。
- `issue verify` 连接 NPU 后提交平台请求；其余命令由本地处理，按需访问平台。`issue preview --full` 读取本地领取回执。
- preview、renew、wait、summarize 需要已有本地领取记录；verify 需要显式指定已领取问题，或使用已关联问题的 deploy job。detail、tasks、regression-preview、status 和 execution show 不要求先领取。

### 领取与续租

| 命令 | 必填参数 | 可选参数与说明 |
|---|---|---|
| `issue claim` | 无 | `--issue-id ID`：省略时由平台分配。自动生成的 request-id 会在请求前输出，**重试需保留** |
| `issue renew` | `--issue-id ID` | 使用本地保存的领取上下文 |

### 查看信息（只读）

| 命令 | 必填参数 | 可选参数与说明 |
|---|---|---|
| `issue preview` | `--issue-id ID` | 查看受影响用例、失败摘要和 seed。`--full`：返回完整领取复现信息和相关知识。**这不是回归候选集** |
| `issue detail` | `--issue-id ID` | 查看生命周期、租约和历史轮次 |
| `issue tasks` | `--issue-id ID`、`--round-no N` | 查询一轮验证 / 回归任务 |
| `issue regression-preview` | `--issue-id ID` | 查看平台回归候选集；只读，不触发回归 |
| `execution show` | `--exec-uuid UUID` | 查看平台执行详情、Shape 和日志；仅接受平台执行 UUID |

### 验证与回归

| 命令 | 必填参数 | 可选参数与说明 |
|---|---|---|
| `issue verify` | `--job-id ID`（deploy job） | `--issue-id ID`：省略时复用部署中记录的问题上下文；部署无上下文时必须显式指定。提交前确保官方客户端已启动 |
| `issue status` | `--issue-id ID`、`--round-no N` | 查询一次；`round-no` 使用 `verify` 的返回值 |
| `issue wait` | `--issue-id ID`、`--round-no N` | `--timeout SECONDS`：默认 3600，必须为有限正数；**agent 建议传 30**。等待期间自动续租，仅在变化时报告进度；超时返回 PLATFORM_WAIT_TIMEOUT，可继续等待同一轮次 |

验证通过后的回归由平台安排，没有单独的 AFXX regression run 命令。

### 提交总结

| 命令 | 必填参数 | 说明 |
|---|---|---|
| `issue summarize` | `--issue-id ID`、`--summary-file PATH`、`--request-id ID` | `--summary-file` 为本地 YAML，建议按 [证据链模板](../skills/afx-issue-summary/assets/summary.yaml) 填写。这是**平台写操作**，不是生成本地总结；修复流程不会自动提交，需人工检视后执行 |

权限、租约及总结格式见 [Agent 调用约定](AGENT_USAGE.md)。

## 10. dry-run 说明

适用命令：`env init/update/check`、`deploy`、`replay`、`doctor`。

| 命令类型 | dry-run 行为 |
|---|---|
| 任务命令（env / deploy / replay） | 跳过远端连接，仍生成本地配置、脚本和请求记录；`deploy` 还会生成源码快照。本地目录见响应中的 `local` |
| `doctor` | 只做本地检查 |

注意：

- dry-run 成功只代表**本地准备完成**，不证明远端环境可用或编译成功。
- 正式执行时去掉 `--dry-run`，**不能复用** dry-run 的 request-id。
- dry-run 不会输入或同步 token。

## 11. MCP 调用

MCP 使用与 CLI 相同的命令路径和参数。

```text
# 查询帮助
afxx_help({"command": "replay"})

# 执行命令
afxx_run({
  "command": "replay",
  "arguments": ["--job-id", "<deploy-job-id>", "--seed", "<seed>", "--dump"]
})
```

- 使用非默认配置时，通过工具的 `config` 字段指定。
- MCP 仅对 `env init` / `env update` 自动开启 `--detach`；其他命令保持 CLI 行为。

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

四个接口不创建 workflow job，与 verify 自动启动共用远端 AFX 的 data_home 解析：优先 AUTOFUSEX_HOME，再读取系统默认目录的 active-home.json，最后回退系统默认目录（Linux 通常为 ~/.local/share/autofusex）。不再派生 workflow-root/afx-client，也不为 issue/worktree 初始化第二个 client。发现当前用户的其他 managed home 或旧版常驻 client 时，status 返回 conflicts，写操作返回 PLATFORM_CLIENT_CONFLICT；不会停止其他目录的进程。该检查用于发现现有冲突，不替代 AFX 对外部并发启动的互斥管理。

status 不启动 daemon、不加载 CANN、不读取平台 token，返回 home、machine_id、daemon_running、state、runs、active_operation 和 conflicts。state 区分 uninitialized、stopped、idle、preparing、running、draining 等实际状态；运行会话不等于可以领取复测，retest_readiness 当前固定为 not_checked。

start 复用已有身份和目录；首次初始化必须有 platform.client_token_file；已有 client 可以复用其保存的凭据。已有 endpoint、设备或 token 不匹配时拒绝覆盖；旧版目录缺失身份配置时返回 PLATFORM_CLIENT_MIGRATION_REQUIRED，不悄悄生成新身份。AFX start 会读取平台配置，可能持续运行 fuzz。stop 请求会话停止，等待在途任务退出后关闭 daemon；已停止时重复调用成功。restart 校验配置后完成同样的停止过程，再在当前配置环境中启动。stop/restart 不删除身份、历史和平台记录，不强杀 worker。超时返回 PLATFORM_CLIENT_TIMEOUT，应先查询 status，不能将超时视为已停止。

--timeout 范围为 (0,3600] 秒，默认 300，控制等待期限；SSH、准备和单次 AFX 请求另有开销。中断本地命令不意味着远端已取消，先查询 status。MCP 使用同名 `client status/start/stop/restart`，参数与 CLI 一致。

平台派发、runner 环境和结果身份问题仍待定位；这些接口只管理生命周期，不保证复测已领取或使用了正确候选包。replay 保留独立入口。
