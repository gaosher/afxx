# AF Forge

本工具从本地工作区同步源码到 NPU 环境，构建和安装 AF，并管理 Python 环境、固定 seed 复跑及 AFX 平台验证。源码同步包含未提交修改，使用 npursync 传输冻结快照。

安装后使用 `af-forge` 命令；`af-forge-mcp` 提供 stdio MCP 接口。命令和远端任务共用同一套业务模块，上传文件从已安装的 wheel 中取得，无需保留本工具源码。源码仓库由配置指定，命令可在任意目录执行。

- [配置说明与默认值](docs/CONFIGURATION.md) · [YAML 模板](af_forge/resources/config.example.yaml)
- [本地依赖](docs/LOCAL_DEPENDENCIES.md)
- [Agent 调用约定](docs/AGENT_USAGE.md) · [问题修复 Skill](skills/af-forge-repair/SKILL.md)
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

编辑生成的配置，填写连接环境、workflow_root 和远端 LLVM/LLD 路径。字段说明、路径推导和 Python/CANN 选项见 [配置文档](docs/CONFIGURATION.md)。

源码目录由配置显式指定，工具目录可以放在仓库之外；部署仍遵循 graph-autofusion 的 build.sh、构建产物和安装约定。

```bash
af-forge doctor
```

doctor 校验配置、本地连接工具和 npussh 登录。远端依赖由 init/check 及各操作的预检负责。

## 初始化和更新环境

```bash
af-forge env init --config ~/.config/af-forge/config.yaml
af-forge env check
# 修改配置后更新 torch_npu / AFX
af-forge env update --config ~/.config/af-forge/config.yaml
```

init 自动上传配置并准备 Python 3.11、CANN 和专用 venv。update 更新 torch_npu/AFX，保留现有 CANN。init/update 默认跟随远端日志；`--detach` 仅提交任务，后续用 status 查询。

首次初始化、包源、版本选择和失败恢复规则见 [环境配置](docs/CONFIGURATION.md#初始化和更新环境)。

## 部署源码与 AF

```bash
af-forge deploy --request-id deploy-001
af-forge status --job-id <job-id>
```

提交返回 job ID。远端先核对配置、当前环境清单、实际 Python 包和 CANN，再同步源码，执行 `bash build.sh --pkg --enable-mlir -j 8`，将本轮产物写入 job 的 packages/，随后校验并升级安装 AF。安装后验证实际加载的 pyautofuse、adapter、codegen 文件与本轮构建产物一致。

环境与配置不一致时输出 WARNING；依赖无法加载或 AF 部署产物被替换仍报错。详见 [环境一致性与冻结配置](docs/CONFIGURATION.md#环境一致性与冻结配置)。

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

先完成 [平台连接配置](docs/CONFIGURATION.md#平台连接)。

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

verify 在远端核对部署记录与实际加载的 AF，确认官方客户端已启动后向 `/api/issues/{id}/verify` 提交 lease_id、fix_version、fix_sha256 和 request_key。job 的 platform.json 保存固定请求和回执。网络失败后重试同一部署会复用请求键；不同修复使用新的部署 job。

status/wait 查询指定 verify 轮次及紧随其后、包身份一致的 regress 轮次。回归仍在执行时不会把验证通过视为整个流程通过；后续修复的轮次不会覆盖本次结果。失败或超时返回错误码及轮次详情，超时后可继续查询同一 round_no。修复总结由平台处理。

### 查询平台定位信息

以下命令只读，不需要先 claim，也不连接 NPU：

```bash
af-forge issue detail --issue-id F152
af-forge issue tasks --issue-id F152 --round-no 1
af-forge issue regression-preview --issue-id F152
af-forge execution show --exec-uuid <exec_uuid>
```

`detail` 返回生命周期、租约、轮次和事件；`tasks` 查询实际验证或回归轮次，返回 issue_id、round_no 和 tasks。回归候选预览不等于最终执行集。`execution show` 使用执行 UUID（不是数值 exec_id），返回环境、Shape、日志及平台的 log_truncated/log_missing 标记；不将截断日志标为完整。修复总结先在本地整理、人工检视，再通过独立的 `issue summarize` 显式发布。

### 检视后提交修复总结

先将已检视的总结保存为本地 `summary.yaml`（直接填写知识字段，不加 knowledge 外层）。YAML 是 Forge 的本地输入格式，便于编辑和检视；平台接收的是 Forge 转换后的 JSON 请求，并不要求 YAML：

```yaml
kind: bugfix_function
title: "修复调度边界条件"
symptom: "原始 seed 在指定 Shape 下编译失败"
root_cause: "边界条件处理不完整"
solution: "修正边界条件，并补充对应回归测试"
fix_ref: "实际修复 commit SHA"
```

```bash
af-forge issue summarize --issue-id F152 \
  --summary-file /absolute/path/summary.yaml --request-id summary-F152-001
```

这是本地发起的平台写操作，不连接 NPU。需要本地领取记录、有效租约、knowledge 权限和平台 summarizing 状态。成功返回 issue_id、status=resolved 和 knowledge_id。网络失败重试同一命令；同一 request-id 固定总结内容、租约和平台，改变内容会报 REQUEST_CONFLICT。verify、回归及修复 skill 都不会自动调用它。

必填字段为 kind/title/symptom/root_cause/solution。kind 使用 AFX 类型，如 bugfix_function、bugfix_accuracy 或 perf_tuning；可选字段包括 keywords、ops、dtypes、backends、graph_pattern、applicability、version_range、fix_ref、body_md。具体类型和长度由平台校验。提交后知识会发布，不只是保存草稿。

## AFX 常驻客户端

Forge 在本地控制远端任务；AFX 官方客户端在 NPU 上独立运行，负责心跳、上传、复测领取和平台更新。`issue verify` 提交前调用官方命令启动客户端，使用配置中的 venv/CANN 和 replay.devices。相同环境的已有进程直接复用；环境或 AF 包变化时报错，不自动停止或重启。Forge 不实现上传、心跳或复测循环。

配置 `platform.client_token_file` 后无需手工启动。客户端文件位于远端 state_dir 的上一级目录下的 `afx-client/`，日志为 `client.log`。也可在独立 HOME 中手工启动官方客户端，例如：

```bash
source /mnt/workspace/af-workflow/Ascend/cann/set_env.sh
source /mnt/workspace/af-workflow/venv/bin/activate
export AUTOFUSEX_HOME=/mnt/workspace/af-workflow/afx
mkdir -p "$AUTOFUSEX_HOME/results"
afx platform client run --endpoint https://autofuse.online \
  --token-file /absolute/path/client.token \
  --results "$AUTOFUSEX_HOME/results/results.sqlite" --retest --device npu:0
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

冻结配置可用于重建环境，操作与约束见 [从冻结配置重建环境](docs/CONFIGURATION.md#从冻结配置重建环境)。

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

提供两个工具：`af_forge_help(command)` 查询命令参数；`af_forge_run(command, arguments, config)` 执行命令并返回原有 JSON 响应，失败同时标记 MCP `isError`。例如 `{"command":"deploy","arguments":["--request-id","fix-001"]}`，随后用 `{"command":"status","arguments":["--job-id","<job-id>"]}` 查询。`arguments` 是参数数组，不经过 shell。

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

顶层命令按用途分为四类，命令名称保持不变：

- **远端环境**：`env init`、`env update`、`env check`
- **远端工作流**：`deploy`、`replay`、`status`
- **问题验证**：`issue claim|preview|detail|tasks|regression-preview|status|wait|verify|renew|summarize`、`execution show`
- **本地操作**：`doctor`、`config init`

`af-forge help` 显示带分类前缀的顶层命令；`af-forge issue --help` 显示问题处理操作，`af-forge config --help` 显示配置操作。旧的连字符子命令不再作为 CLI 入口。

MCP 使用相同命令路径：`af_forge_help({"command":"issue"})` 查看分组，`af_forge_help({"command":"issue verify"})` 查看操作参数；`af_forge_run({"command":"issue claim"})` 执行操作。参数放在 `arguments` 数组中，例如 `{"command":"issue verify","arguments":["--issue-id","F152","--job-id","<job-id>"]}`。JSON 响应的 `command` 使用空格分隔路径；任务记录中的内部操作标识保持不变。

## 问题修复 Skill

`af-forge-repair` 编排领取、复现定位、修改、deploy、verify、回归和修复提交，最终生成带修复及验证证据的可提交 summary.yaml，供人工检视后使用 issue summarize 发布。使用现有 Forge CLI/MCP，不另实现客户端。安装到 Codex 的 skills 目录（目标已存在时先检查本地修改）：

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R skills/af-forge-repair "${CODEX_HOME:-$HOME/.codex}/skills/"
```

调用示例：`使用 $af-forge-repair 修复 AFX issue F152，验证和回归通过后 commit，等待检视。`
