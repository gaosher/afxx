# AFXX — AFX Extensions

AFXX 是适配 AFX 的开发与问题修复工具包，从本地工作区同步源码到 NPU 环境，构建和安装 AF，并管理 Python 环境、固定 seed 复跑及 AFX 平台验证。源码同步包含未提交修改，使用 npursync 传输冻结快照。

安装后使用 `afxx` 命令；`afxx mcp` 提供 stdio MCP 接口。命令和远端任务共用同一套业务模块，上传文件从已安装的 wheel 中取得，无需保留本工具源码。源码仓库由配置指定，命令可在任意目录执行。

- [命令参考与参数矩阵](docs/afxx-commands.md)
- [配置说明与默认值](docs/CONFIGURATION.md) · [YAML 模板](afxx/resources/config.example.yaml)
- [Agent 调用约定](docs/AGENT_USAGE.md) · [问题修复 Skill](skills/afx-issue-repair/SKILL.md) · [修复总结 Skill](skills/afxxsummary/SKILL.md)
- [设计与模块边界](docs/DESIGN.md)

## 工具目录

```text
afxx/
├── README.md
├── pyproject.toml          # wheel、依赖和命令入口
├── afxx/               # Python 包
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
│   │   ├── snapshot.py
│   │   ├── python.py
│   │   ├── cann.py
│   │   └── package_sources.json
│   └── afx/                # AFX 接口适配
│       ├── replay.py
│       └── platform.py
├── docs/                   # Agent 规范、设计、本地依赖
└── tests/                  # 单元与接口测试
```

内部模块通过 `afxx` 包导入；远端任务保留相同包结构。日常调用已安装的 `afxx`；agent 也可通过 `afxx mcp` 使用同一组命令。

查看全部命令：`afxx help`。

## 安装与配置

本地需准备 **Python 3.11+（含 pip）、Git、Node.js 20+、npussh/npursync、OpenSSH 客户端和 rsync**，并确保命令在 PATH 中可用。npursync 随 npussh 提供；默认安装仅依赖 PyYAML；MCP SDK 通过 `afxx[mcp]` 按需安装。版本要求、安装方式及检查范围见 [本地依赖说明](docs/LOCAL_DEPENDENCIES.md#本地启动环境)。

安装 AFXX 并登录 NPU 服务：

```bash
python3 -m pip install /absolute/path/afxx-1.0-py3-none-any.whl
npussh login
npussh list
```

生成本地配置，默认保存到 `~/.config/afxx/config.yaml`：

```bash
afxx config init --source /absolute/path/graph-autofusion
```

可用 `afxx config init --output` 指定其他位置。使用非默认位置时，后续命令需显式传 `--config /absolute/path/config.yaml`。
所有命令默认读取 `~/.config/afxx/config.yaml`；仅 `--config` 显式覆盖。

编辑生成的配置，填写连接环境、workflow_root 和远端 LLVM/LLD 路径。字段说明、路径推导和 Python/CANN 选项见 [配置文档](docs/CONFIGURATION.md)。

源码目录由配置显式指定，工具目录可以放在仓库之外；部署仍遵循 graph-autofusion 的 build.sh、构建产物和安装约定。

```bash
afxx doctor
```

doctor 校验配置、本地连接工具和 npussh 登录。远端依赖由 init/check 及各操作的预检负责。

## 命令与参数矩阵

完整命令速查、必填参数、默认值、ID 类型、dry-run 和 MCP 行为见 [命令参考](docs/afxx-commands.md)。也可执行 `afxx <命令> --help` 查看当前安装版本的参数。

## 初始化和更新环境

```bash
afxx env init
afxx env check
# 修改配置后更新 torch_npu / AFX
afxx env update
```

init 自动上传配置并准备 Python 3.11、CANN 和专用 venv。update 更新 torch_npu/AFX，保留现有 CANN。init/update 默认跟随远端日志；`--detach` 仅提交任务，后续用 status 查询。

配置 platform 后，init 优先复用本地 token 文件，缺失时在交互终端隐藏输入；环境成功后自动同步到 NPU。路径留空时使用 `~/.config/afxx/auth/afx.token` 和 `<workflow_root>/auth/afx.token`。非交互调用需预先准备本地文件；detach/MCP 提交后通过 status/wait 完成成功后的同步。

首次初始化、包源、版本选择和失败恢复规则见 [环境配置](docs/CONFIGURATION.md#初始化和更新环境)。

## 部署源码与 AF

```bash
afxx deploy --request-id deploy-001
afxx status --job-id <job-id>
```

提交返回 job ID。远端先采集实际环境快照、检查运行依赖，再同步源码，执行 `bash build.sh --pkg --enable-mlir -j 8`，将本轮产物写入 job 的 packages/，随后校验并升级安装 AF。安装后验证实际加载的 pyautofuse、adapter、codegen 文件与本轮构建产物一致。

日常任务现场采集环境快照，不比对历史安装清单或检查缓存 wheel；依赖无法加载或 AF 部署产物被替换仍报错。详见 [Job 环境快照](docs/CONFIGURATION.md#job-环境快照)。

`deploy --issue-id F152` 关联领取上下文，在编包期间续租。部署完成后，通过独立的 `issue verify` 命令提交平台验证。

```bash
# 离线校验并生成本地快照
afxx deploy --dry-run

# 查看远端任务日志
npussh <environment> 'tail -n 100 /remote/workflow-state/<job-id>/job.log'
```

同步包含受版本控制文件及未忽略的新文件，排除凭据、agent 私有配置、构建目录和依赖缓存。每次生成完整目录快照及哈希清单。删除操作仅处理上一份托管清单拥有的源码文件。`.af-workflow` 标识远端源码归属，手工修改托管文件会阻止同步。

同一源码目录的同步/编译串行，安装与受管理的复测通过锁协调。部署直接在专用 build 目录增量编译和覆盖产物，旧版 build 指纹会在下次部署时移除。AF 覆盖安装失败会使当前部署失效；Python 更新支持锁文件回滚，AF/CANN 安装失败需检查现场并恢复安装。

## 随机 fuzz 测试

```bash
# 使用已初始化的远端 venv/CANN 和当前已安装的 AF，运行 100 个随机用例
afxx fuzz run --count 100
# 省略 --count 持续运行；两种方式均异步返回 job_id
afxx fuzz run --device npu:0 --device npu:1 --workers 4

afxx status --job-id <job-id>        # AFXX 任务状态、日志和结果路径
afxx fuzz status --job-id <job-id>   # AFX 官方队列、supervisor 和 campaign 状态
afxx fuzz stop --job-id <job-id>     # 请求 AFX 停止；随后查询状态确认结束
```

AFXX 直接调用所选 venv 中的 `python -m afx fuzz run --fg --save`，加载配置指定的 CANN，在远端 job 中等待官方 supervisor。默认设备和资源预算由 AFX 自动选择；可传 `--count`、重复的 `--device` / `--backend`、`--workers`、`--compile-threads` 和 `--ops`。省略 `--count` 表示持续运行。关闭本地终端不会停止远端任务。

所有 fuzz 任务复用 `<workflow_root>/fuzz/`，作为 AFX home 和工作目录。其中 `results/results.sqlite` 累积各轮所有设备与后端结果，`tasks/tasks.sqlite` 由 AFX 管理调度；无需手动创建空数据库。fuzz 与 replay、平台复测目录保持独立，不按 job 或卡额外建结果目录。配置、实际环境快照和进程日志仍保存在 `workflow-state/<job-id>/`。fuzz 不自动上传结果或下载全量现场。

`status` 的 success 表示 AFX 进程正常结束，不代表每个随机用例通过；用 `fuzz status` 查看 AFX 队列结果。停止也是官方的请求式停止，并非强制杀进程。持续 fuzz 持有环境共享锁，deploy/update 会等待；更换 AF 或 Python 依赖前应先停止对应 fuzz 任务。同一 fuzz 目录一次只允许一个运行，重复启动的远端任务以 `FUZZ_ALREADY_RUNNING` 失败，不排队。前一轮结束后复用同一数据库；旧 job 不能查询或停止已由新 job 接管的 AFX 运行（`FUZZ_JOB_REPLACED`），历史任务记录仍可通过普通 `status` 查询。

## AFX 平台修复流程

先完成 [平台连接配置](docs/CONFIGURATION.md#平台连接)。

### 直接领取平台编号

平台提供 `/api/issues` 且 agent token 拥有 `issue` 权限时，可以直接使用功能、精度或性能问题编号（F、A、P 加 1 至 9 位数字）。

```bash
afxx issue claim --issue-id F152
afxx issue preview --issue-id F152
afxx issue renew --issue-id F152
afxx deploy --issue-id F152 --request-id deploy-F152-1
# 用 status --job-id 确认这次部署成功后提交
afxx issue verify --issue-id F152 --job-id <deploy-job-id>
```

领取成功后终端显示问题摘要。完整回执保存在本机 `issues/<issue_id>.json`；编号问题的 preview 默认输出缩进 JSON，包含失败摘要和去重后的全部 nnsmith seed。若领取回执被截断，preview 会从平台分页读取当前受影响的用例。加 `--full` 可查看领取时保存的复现详情和相关知识。编号领取可显式传 `--request-id`；省略时 CLI 在发送前将生成的 ID 写入 stderr，重试使用同一个 ID。修复期间按租约到期时间提前续租，关联部署期间 workflow 自动续租。验证以实测 AF 版本及 pyautofuse.so 哈希提交，返回 `data.round_no`。平台按芯片和包身份调度已注册客户端。

### 平台分配与结果查询

省略 `--issue-id` 时，平台分配一个待修复问题；没有任务时返回 `data=null`。

```bash
afxx issue claim --request-id claim-001
# 使用 verify 返回的 round_no
afxx issue status --issue-id F152 --round-no 1
afxx issue wait --issue-id F152 --round-no 1 --timeout 3600
```

verify 在远端核对部署记录与实际加载的 AF，确认官方客户端已启动后向 `/api/issues/{id}/verify` 提交 lease_id、fix_version、fix_sha256 和 request_key。job 的 platform.json 保存固定请求和回执。网络失败后重试同一部署会复用请求键；不同修复使用新的部署 job。

status/wait 查询指定 verify 轮次及紧随其后、包身份一致的 regress 轮次。回归仍在执行时不会把验证通过视为整个流程通过；后续修复的轮次不会覆盖本次结果。失败或超时返回错误码及轮次详情，超时后可继续查询同一 round_no。修复总结由平台处理。

### 查询平台定位信息

以下命令只读，不需要先 claim，也不连接 NPU：

```bash
afxx issue detail --issue-id F152
afxx issue tasks --issue-id F152 --round-no 1
afxx issue regression-preview --issue-id F152
afxx execution show --exec-uuid <exec_uuid>
```

`detail` 返回生命周期、租约、轮次和事件；`tasks` 查询实际验证或回归轮次，返回 issue_id、round_no 和 tasks。回归候选预览不等于最终执行集。`execution show` 使用执行 UUID（不是数值 exec_id），返回环境、Shape、日志及平台的 log_truncated/log_missing 标记；不将截断日志标为完整。修复总结先在本地整理、人工检视，再通过独立的 `issue summarize` 显式发布。

### 检视后提交修复总结

先将已检视的总结保存为本地 `summary.yaml`（直接填写知识字段，不加 knowledge 外层）。YAML 是 AFXX 的本地输入格式，便于编辑和检视；平台接收的是 AFXX 转换后的 JSON 请求，并不要求 YAML：

```yaml
kind: bugfix_function
title: "修复调度边界条件"
symptom: "原始 seed 在指定 Shape 下编译失败"
root_cause: "边界条件处理不完整"
solution: "修正边界条件，并补充对应回归测试"
fix_ref: "实际修复 commit SHA"
```

```bash
afxx issue summarize --issue-id F152 \
  --summary-file /absolute/path/summary.yaml --request-id summary-F152-001
```

这是本地发起的平台写操作，不连接 NPU。需要本地领取记录、有效租约、knowledge 权限和平台 summarizing 状态。成功返回 issue_id、status=resolved 和 knowledge_id。网络失败重试同一命令；同一 request-id 固定总结内容、租约和平台，改变内容会报 REQUEST_CONFLICT。verify、回归及修复 skill 都不会自动调用它。

必填字段为 kind/title/symptom/root_cause/solution。kind 使用 AFX 类型，如 bugfix_function、bugfix_accuracy 或 perf_tuning；可选字段包括 keywords、ops、dtypes、backends、graph_pattern、applicability、version_range、fix_ref、body_md。具体类型和长度由平台校验。提交后知识会发布，不只是保存草稿。

修复交付使用 [总结模板](skills/afxxsummary/assets/summary.yaml)：正文按独立根因展示实际错误 IR、错误机制、修复行为和验证依据，补充人工检视重点与同类问题定位指引。验证依据随根因说明，末尾保留一行平台索引；部署追踪信息留在任务记录。填写规则见 [Agent 调用约定](docs/AGENT_USAGE.md)。

## AFX 常驻客户端

AFXX 使用 AFX >= 0.2.3 的 managed client 生命周期。`issue verify` 提交前，在配置的 venv/CANN 环境下首次调用 `afx init`，随后调用 `afx start`，等待 `afx status` 中的执行会话进入 running；失败则不提交 verify。AFX 负责进程、任务队列、心跳、上传和复测领取。启动会读取平台配置，也可能开始 fuzz，并非仅启动复测监听。

配置 `platform.client_token_file` 为已有远端 token 文件。AFXX 在远端进程内读取 token，避免将秘密放进系统命令参数；AFX 将其保存到自己管理的 client 配置中。设备沿用 replay.devices（auto 时查询可见卡）。客户端目录为 remote.state_dir 上一级的 `afx-client/`；AFXX 调用日志为 `client.log`，执行日志由 AFX 保存在 `runs/<run-id>/execution.log`。

可在远端使用同一 Python/CANN 环境操作已有 managed client，例如（路径按实际配置调整）：

```bash
source /mnt/workspace/af-workflow/Ascend/cann/set_env.sh
source /mnt/workspace/af-workflow/venv/bin/activate
afx status --data-dir /mnt/workspace/af-workflow/afx-client
afx stop --data-dir /mnt/workspace/af-workflow/afx-client
afx start --data-dir /mnt/workspace/af-workflow/afx-client
# 更新包后，需要刷新驻留执行进程时：
afx restart --data-dir /mnt/workspace/af-workflow/afx-client
```

stop/restart 的排空和退出行为由 AFX 决定；AFXX 不自行 kill、重启或刷新驻留库。已有 client 的 endpoint、token 或设备与配置不符时直接报错，不覆盖已有设置。迁移时如有旧 `platform client run` 存活，须先停止旧进程，防止重复领取任务。

本次只接入 client 生命周期。AFXX replay（包括任意 seed 和 `--dump`）仍使用原入口，不进入 managed 队列；deploy 仍执行已有环境占用检查。`start` 复用会话不意味着更新了已加载的 AF 库，启动成功也不代表平台回归通过，仍需通过 issue status/wait 查询结果。

### 遗留问题：手动 replay 临时占用单卡

状态：待实现（2026-11-20）。当前平台复测与手动 replay 通过设备锁竞争，没有手动 replay 优先级保证。

预期平时所有卡都可执行平台任务。手动 replay 到来时，优先使用空闲卡；否则选定一张卡，暂停向该卡启动新任务，等待当前任务结束后执行 replay，其他卡继续工作。replay 完成、失败或取消后自动恢复该卡的平台任务。

AFX 需要提供按卡独立执行和本机让卡协调，AFXX 调用该能力并展示等待状态。让卡本身在本机完成；已领取但未执行的平台任务需要明确续租或归还策略，优先评估按空闲卡领取、避免预取积压任务的方式。平台接口是否需要扩展待实现时核实。共享 AF 安装目录的 deploy 仍需协调所有正在使用该包的任务。


## 独立 seed 复跑

```bash
afxx replay \
  --job-id <deploy-job-id> --seed <seed>

afxx replay \
  --job-id <deploy-job-id> --seeds-file /absolute/path/seeds.txt
```

seed 文件每行一个 seed。replay 校验当前部署、环境清单和加载库，创建独立 HOME、缓存和结果数据库。单 seed 使用第一张配置卡，批量模式使用配置的 workers、设备列表和 timeout。

Shape 数量由 AFX 解码 seed 得到：静态为 1，动态为 2–8。结果通过 `fuzz maintenance results` 分页读取及查询 attempt 详情，不直接读取 SQLite 表。

通过要求为执行命令成功、CPU/AF-MLIR attempt 均为 ok、seed/backend/shape 完整且无重复。AF-MLIR correctness 必须为 pass，CPU 参考后端允许 not_checked 或 pass。unsupported、缺失结果和目标后端未比较均判失败。

独立 replay 默认只保存结果。设置 `upload.enabled: true` 或传 `--upload` 后，调用官方 `afx platform client once` 一次性上传本次结果并发送心跳；`--no-upload` 可覆盖。使用 `platform.url` 和远端 `platform.client_token_file`（未指定时复用 `platform.remote_token_file`），不启动常驻 client、不领取复测、不执行平台更新。上传游标由 AFX 保存在本次结果目录的 `platform-sync.sqlite`。

`result.json` 中 `upload=completed` 表示一次性命令及回执检查完成，`upload=failed` 表示上传调用失败；另存 `upload_exit_code` 和 `upload_receipt`。AFX 回执的 `uploaded` 是本轮处理数，可能包含拒绝或跳过项，不能当作全部接收成功的证明；服务端接收情况需查平台或 AFX 上传状态。旧 `upload.sync_config_path` 已移除，配置中应删除该字段，不再复制凭据到结果目录。平台客户端仍会上传自己领取的验证任务结果。

## 环境清单、产物与清理

本地状态根目录由 `local.state_dir` 指定，默认是 `~/.local/state/graph-autofusion/dev_workflow/jobs`：

```text
<local.state_dir>/
├── requests/                    request-id → 冻结任务的幂等索引及锁
├── issues/
│   ├── <issue-id>.json          领取响应、目标环境及租约
│   └── <issue-id>/summary.yaml  skill 生成、待人工检视的修复总结
└── <job-id>/
    ├── config.yaml             本地提交时冻结的配置
    ├── request.json            请求及提交记录
    ├── source.json             deploy 源码清单、哈希及快照清理结果
    ├── snapshot/               待同步源码的普通目录树，启动确认后清理
    └── afxx/               随任务上传的执行模块
```

远端默认布局如下；显式配置路径时以实际配置为准。job 内文件按任务类型和执行阶段生成，不是每个任务都有全部文件：

```text
<workflow_root>/
├── python/                     托管 Python 及引导工具
├── venv/                       专用 Python 环境
├── Ascend/cann/                CANN 安装目录
├── auth/afx.token              默认平台凭据，不属于 job 产物
├── graph-autofusion/           同步后的源码
│   ├── .af-workflow            托管源码归属和哈希清单文件
│   └── build/                  复用的编译目录
├── workflow-state/<job-id>/
│   ├── config.yaml            提交配置；init/update 成功后附带环境锁
│   ├── environment.json       实际环境、采集错误及 Python 依赖检查
│   ├── request.json           任务请求
│   ├── launch.json            远端启动记录
│   ├── status.json            当前阶段与终态
│   ├── source.json            源码清单及快照清理结果
│   ├── snapshot/              同步校验成功后清理
│   ├── packages/              本轮 AF run 包
│   ├── result.json            部署/replay/环境操作结果及产物索引
│   ├── platform.json          verify 固定请求及平台回执
│   ├── job.log                任务日志
│   └── uv.log                 Python 包安装日志
├── fuzz/               # 随机 fuzz 的 AFX home、调度库和结果库
├── workflow-results/<job-id>/
│   ├── results.sqlite         AFX replay 结果数据库
│   ├── dump/                  AF/GE 图、MLIR 等定位产物
│   └── cache/                 编译缓存；其他日志/产物按实际执行生成
└── workflow-env-state/
    ├── current.json           当前环境清单
    ├── wheels/                按哈希保存的 torch_npu/AFX wheel
    └── locks/                 Python 依赖锁文件
```

replay 终态下载的证据在本机 `/tmp`，响应中的 `evidence_path` 给出准确路径：

```text
/tmp/afxx-evidence-<uid>/<job-id>-<环境标识>/
├── evidence.json               完整清单、远端/本地路径、大小及哈希
├── job/                        下载的报告、任务日志及调试文件
└── results/                    下载的完整 dump、日志、缓存和结果数据库
```

CANN 全量安装包临时下载到 **远端** `/tmp/af-cann-<job-id>-*/`，与本地 evidence 目录不同。

源码快照 snapshot/ 为普通目录树。确认远端启动后清理本地副本，远端同步校验成功后清理远端副本。状态不明确时保留现场，dry-run 保留快照。源码清单和部署引用继续用于校验，删除快照后需另行保留源码才能恢复未提交修改。

完整 CANN 下载包在安装成功后清理，失败时保留。AF run 包、共享 wheel、锁文件和任务记录持续保留，按需要归档。当前部署注册表通过路径和 SHA-256 引用 result.json，后者引用 source.json；后续复跑依赖这些文件。

init/update 成功后的冻结配置可用于重建环境，操作与约束见 [从冻结配置重建环境](docs/CONFIGURATION.md#从冻结配置重建环境)。

## 验证工具本身

```bash
python3 -m pytest tests -q
```

测试使用临时目录、Git/rsync 和构建、平台、设备接口夹具，覆盖同步、幂等、更新回滚、环境冻结、结果判定和平台验证轮次。真实编包、NPU 执行及平台联调需在配置的目标环境验证。

测试分层：业务规则和边界在所属模块测试；deploy/replay 编排覆盖成功路由与各阶段失败，不对无关开关做全组合；CLI 验证公开路由和响应；MCP 只验证 stdio 握手、帮助、命令执行及错误封装。各命令共用的远端 bundle 只做一次独立导入验证。幂等、并发、回滚、源码保护和结果判定仍保留故障场景覆盖。

发布前必须指定 AFX 源码运行完整测试及真实契约测试：

```bash
AFX_SOURCE=/absolute/path/Autofuse-Debugger python3 -m pytest tests -q --require-afx-contracts
```

日常测试未设置 AFX_SOURCE 时跳过真实契约；发布验证的 `--require-afx-contracts` 在缺少源码路径时直接失败，不能将跳过当成兼容通过。

契约测试需要 pytest、PyYAML、jsonschema 及对应 AFX 接口的运行依赖；调用真实的 AFX seed、结果 CLI 和修复服务逻辑，使用临时数据库，不连接生产平台。

## MCP 接入

先在运行 AFXX 的 Python 环境安装 MCP 可选依赖：

```bash
python3 -m pip install 'afxx[mcp]'
```

MCP 是可选接入方式：agent 可直接执行 `afxx` CLI，也可通过 MCP 调用同一套功能。`afxx mcp` 在本地由 agent 客户端启动，通过标准输入/输出（stdio）通信；不需要在 NPU 上启动 MCP 服务，也不需要配置 HTTP 地址。

### 接入 Codex

安装 AFXX wheel 后，在本地终端执行：

```bash
# 查找 wheel 提供的 MCP 入口
command -v afxx

# 将上一条输出的绝对路径填到这里
codex mcp add afxx -- /absolute/path/to/afxx mcp
codex mcp list
```

默认使用 `~/.config/afxx/config.yaml`。如果要固定使用另一份配置，注册时将参数传给 afxx mcp：

```bash
codex mcp add afxx -- /absolute/path/to/afxx mcp --config /absolute/path/config.yaml
```

两种注册方式选一种。注册后，在加载了该 MCP 的 agent 会话里请求：“使用 afxx MCP 查看 deploy 命令帮助”。应调用 `afxx_help`，无需执行真实部署。`codex mcp list` 只确认注册信息，工具调用成功才表示接入可用。

绝对路径用于让客户端找到可执行文件；若 command 只写 `afxx`，客户端进程的 PATH 必须包含它所在的目录，args 中填写 `mcp`。实际远端操作还要求客户端环境能找到 npussh 等连接工具。

### 其他支持 stdio 的客户端

按客户端自己的 MCP 配置格式填写可执行文件路径。采用 mcpServers JSON 格式的客户端可参考下例；这段 JSON 不用于 Codex 的 config.toml：

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

如需指定其他配置，将 args 改为 `["mcp", "--config", "/absolute/path/config.yaml"]`。无需手动在终端常驻运行 afxx mcp，客户端负责启动它。

提供两个工具：`afxx_help(command)` 查询命令参数；`afxx_run(command, arguments, config)` 执行命令并返回原有 JSON 响应，失败同时标记 MCP `isError`。例如 `{"command":"deploy","arguments":["--request-id","fix-001"]}`，随后用 `{"command":"status","arguments":["--job-id","<job-id>"]}` 查询。`arguments` 是参数数组，不经过 shell。

`init/update` 通过 MCP 自动使用 `--detach`，不会占住会话等待远端安装；其他命令保持 CLI 行为。取消 MCP 调用停止本地 CLI 子进程，已提交的远端任务仍可通过 job ID 查询。标准输出仅用于 MCP 协议，诊断日志写到标准错误。只有公开命令可通过 MCP 调用；内部 worker 入口不开放。MCP 不额外增加平台行为或自动提交 verify。

## 构建 wheel

```bash
uv venv --python 3.11
uv pip install -e '.[dev,mcp]'
.venv/bin/python -m pytest tests -q
.venv/bin/python -m build
```

产物位于 `dist/`。wheel 包含远端运行模块、包源配置和 YAML 模板，不包含用户配置、token、源码快照或本地状态。本工具采用 [Apache License 2.0](LICENSE)。

## 发布到 PyPI

发布工作流位于 [publish.yml](.github/workflows/publish.yml)，推送 `v*` tag 时触发。流程检查 tag 与 `pyproject.toml` 版本一致，运行完整测试（包括固定 AFX commit 的契约测试），构建并检查 wheel/sdist，验证 wheel 安装，最后上传 PyPI。普通分支推送不会发布。

首次发布前需配置：

1. GitHub 仓库 `gaosher/afxx` 的 Settings → Environments 中创建 `pypi`。
2. 在 [PyPI Publishing](https://pypi.org/manage/account/publishing/) 添加 GitHub Trusted Publisher：项目 `afxx`、Owner `gaosher`、Repository `afxx`、Workflow `publish.yml`、Environment `pypi`。已有项目则在项目的 Publishing 页面配置。无需保存 PyPI token；首次发布时包名必须可用。

提交代码与版本号后发布，例如 `pyproject.toml` 中版本为 `1.0`：

```bash
git push -u origin main
git tag v1.0
git push origin v1.0
```

到 GitHub Actions 查看结果。后续发布需递增版本号，不覆盖已发布文件。AFX 契约源码通过 HTTPS 从 GitCode 拉取，版本固定在工作流的 `AFX_REVISION`；更新该值时需重新验证契约测试。源码获取或测试失败都会阻止发布。

## 问题修复 Skill

`afx-issue-repair` 编排领取、复现定位、修改、deploy、verify、回归和修复提交，完成后交给 `afxxsummary` 生成待人工检视的 summary.yaml。`afxxsummary` 也可独立重写已有总结，不修改代码、不触发部署/复测、不发布平台。使用现有 AFXX CLI/MCP，不另实现客户端。通过 CLI 将随 wheel 发布的两个 skill 安装到 Codex 用户目录：

```bash
afxx skills install --agent codex
```

目标为 ~/.agents/skills/ 下的 afx-issue-repair 和 afxxsummary，无需工作流配置或远端连接。重复安装相同内容会跳过；已有目录内容不同时返回 SKILL_CONFLICT，先保留并移走旧目录，再重新安装。pip 升级后重新执行此命令即可检查 skill 是否需要更新。Codex 发现后可调用 $afx-issue-repair 或 $afxxsummary；未出现时重启 Codex。

调用示例：`使用 $afx-issue-repair 修复 AFX issue F152，验证和回归通过后 commit，等待检视。`

独立总结：`使用 $afxxsummary 根据 F152 的修复提交和现有证据重新生成 summary.yaml，不提交平台。`

replay 到达 success/failed 后，本地 `status` 或 `status --wait` 自动将完整 dump、日志、报告、结果数据库和清单下载到 `/tmp/afxx-evidence-<uid>/<job-id>-<环境标识>/`。返回仅包含 job_id、state、evidence_path 和 failures（attempt_id、seed、backend、shape_indices、stage、简短 error）；失败超过 20 项时附数量和截断标记。异步提交后仍需调用 status/wait，远端不会主动推送文件。重复查询校验并复用本地文件，不重复下载。 单个执行可用 `status --attempt <id> --fields ...` 按字段查询。详见 [证据读取说明](docs/AGENT_USAGE.md#status-概要字段查询与等待)。

定位复现可使用 `afxx replay --dump --job-id <deploy-job-id> --seed <seed>`；`--dump` 仅覆盖本次任务并保存到 job 配置，原配置文件不变。
