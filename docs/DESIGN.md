# 简介

## 目的

定义 AFXX的环境、部署、复跑及平台 issue 接口，供维护者和 agent 调用方理解状态、并发和恢复边界。操作命令见 [README](../README.md)，响应和重试规则见 [AGENT_USAGE](AGENT_USAGE.md)。

## 范围

实现位于独立 afxx 仓库，调用仓库 build.sh、CANN 安装包、uv 和 AFX。覆盖环境初始化与更新、源码快照同步、AF 编包安装、固定 seed 复跑、平台 issue 验证及专用复测进程管理。

# 总体概述

## 软件概述

### 项目介绍

本地源码由 local.source_dir 显式指定，不依赖脚本与仓库的位置关系。工作区冻结为目录快照，经 npursync 传至远端任务目录。远端按清单同步专用源码树，构建 AF run 包并安装，记录实际加载文件身份。平台验证通过独立命令提交，由 AFX 平台派发用例，本地 agent 查询验证及回归结果。

### 产品环境介绍

本地 Python 3.11+ 和 PyYAML 读取配置，npussh/npursync 连接 NPU。远端 Linux 提供任务启动用 Python 3.9+、curl、驱动、构建工具和 LLVM/LLD；工具自动下载并校验独立 Python 3.11。workflow_root 推导 CANN、venv、源码、环境状态及任务目录。uv 管理 Python 依赖，AFX 提供执行、设备发现、心跳和平台复测接口。

配置的 platform 地址与 token 文件路径确定平台连接。凭据内容在各自使用端读取。平台按 NPU 架构、后端、AF 版本和库哈希匹配复测任务。

## 软件功能

- init 准备 CANN 和专用 venv，update 更新 torch_npu/AFX。
- deploy 冻结并同步源码，构建安装 AF，验证加载来源。
- replay 在指定有效部署上执行固定 seed，汇总完整性与正确性。
- issue 命令领取、冻结目标查询、续租、提交复测和结果轮询。
- status 查询远端任务及产物；AFX 官方客户端独立运行。

## 设计约束

构建采用 `--pkg --enable-mlir`，默认并行度为 8。AF 安装采用 `--upgrade --pylocal --quiet`。源码目标为专用目录，配置路径与状态目录避开源码树。环境包身份及源码内容在任务边界冻结。C++ ABI、图优化和代码生成算法由 graph-autofusion 自身维护。

## 假设和依赖关系

NPU 驱动可用，用户具备目标目录权限。包源提供配置要求的 Python/CANN 版本。AFX 接口与已安装版本匹配。平台 token 权限满足操作要求，客户端可连接平台。固定 seed 所需生成器、模型描述和运行依赖仍可获取。

# 需求分析与设计

## 整体介绍

入口解析 YAML 和命令，调用远端任务协议或平台适配器。任务保留独立配置、请求和状态。环境清单存于 env-state，供 init/update 管理安装。deploy/replay/check 保留提交配置并现场采集 environment.json，不比对历史清单或检查缓存 wheel；快照用于追溯，不承诺精确重建。AFX 官方客户端在 NPU 独立常驻，AFXX 在 verify 前通过远端启动器调用官方命令。

## 功能需求

### 功能需求 1 环境管理

1. 介绍。创建可追溯、可按锁恢复的 Python/CANN 环境。
2. 输入。python_env、cann 配置以及可选 environment_lock。
3. 处理。init 检查 CANN 完整性，空根目录安装所需组件，再创建 venv 并解析依赖。update 比较 torch_npu/AFX 来源，保持稳定依赖版本和来源，失败时恢复原锁。CANN 更新由独立运维处理。
4. 输出。current.json 保存当前状态、包版本、来源与哈希；共享缓存保存 wheel 和锁。job 保存结果、日志和回滚证据。

### 功能需求 2 源码部署

1. 介绍。将当前本地源码构建为可核验的远端安装。
2. 输入。有效配置、工作区文件、可选 issue 租约及 request-id。
3. 处理。生成快照并上传，核对环境与占用情况，同步源码，编包、安装、核验实际加载来源。
4. 输出。result.json、AF run 包、源码清单和任务状态。平台验证通过独立 issue verify 提交。

### 功能需求 3 独立复跑

1. 介绍。校验指定部署上的固定 seed。
2. 输入。result.json 路径、单 seed 或 seed 文件、设备及上传选项。
3. 处理。持环境共享锁检查部署和加载身份，创建独立结果目录，调用 AFX 单次或批量 CLI，核对 seed/backend/shape 集合。
4. 输出。result.json、结果数据库、日志及可选同步请求状态。

### 功能需求 4 平台验证

1. 介绍。衔接平台 issue、修复部署和用例复测。
2. 输入。平台配置、issue 编号、租约、部署 job、验证 round_no。
3. 处理。本地调用 issues claim/详情/renew；远端 verify 校验 AF 身份并冻结请求；本地轮询指定 verify 及其后续 regress。AFX 官方客户端独立领取任务与上传结果。
4. 输出。本地领取记录、job 的 platform.json、平台验证与回归轮次详情。

## 非功能需求

### 可维护性

入口只负责 CLI 解析、JSON 响应和命令分发。配置、源码同步、运行环境、部署/复测和远端作业按职责分层；共享异常、原子文件操作、进程调用和文件锁位于 afxx.common。各模块显式导入实现所属模块，不反向导入入口，不通过入口重导出工具函数。字段归一化集中于配置加载器。复跑的 seed/命令/公共结果查询契约集中于 afxx.afx.replay，平台 HTTP 由 afxx.afx.platform 负责。

### 可测试性

模块依赖测试拒绝循环依赖和对 CLI 入口的反向导入。任务上传测试在隔离解释器中导入复制后的全部模块，并验证依赖检查子进程可独立执行。

pytest 以临时仓库和目录验证 Git/rsync 行为，通过构建、网络、设备和进程夹具覆盖正常流程及故障恢复。真实 NPU 和平台由独立集成验证覆盖。

### 可移植性

本地支持 macOS/Linux，远端依赖 Linux /proc、文件锁、CANN 和 NPU。Python 3.11 用于专用 venv；基础 Python 和工具链路径可配置。设备型号由运行环境探测，OPS 映射来自 package_sources.json。

### 可靠性

原子文件替换记录阶段状态。request-id、远端启动锁和启动凭据约束任务重试。同步删除仅覆盖托管文件。环境锁保护更新、安装与复测。AF 安装前只检查本次包共享库的占用；init/update 保留 venv 占用检查。

### 特性交叉影响

| 场景 | 适用性 | 结论 |
|---|---|---|
| SuperKernel Python 接口 | 间接适用 | AF 打包交付可能涉及同包组件，fuzz 验证范围限定为所选后端 |
| SuperKernel C++/AOT 接口 | 不涉及接口变更 | workflow 调用既有构建和安装入口 |
| Autofuse 图优化 | 不涉及算法变更 | 源码快照决定编译内容 |
| Autofuse Codegen/Backend | 适用 | 编包启用 MLIR，安装后核验 codegen 库来源 |
| AscendC API / Runtime | 间接适用 | AFX 执行设备用例，workflow 管理环境及进程生命周期 |
| Python/C++ 混合绑定 | 适用 | 检查导入归属及 pyautofuse.so 加载身份 |
| 构建与打包 | 适用 | 保留 build.sh 构建语义，隔离本轮输出与构建缓存 |
| 测试与覆盖率 | 适用 | pytest 覆盖流程，NPU ST 验证执行能力 |
| 性能与日志 | 适用 | 快照哈希、包校验和客户端轮询增加外围开销 |
| 兼容性 | 适用 | YAML、CLI、AFX 协议和安装布局构成兼容边界 |

## 性能

### 编译时长

快照及校验按文件数量和总字节数线性处理，部署复用已有的专用 build 目录，由 build.sh 和 CMake 配置、更新构建产物。编译并行度由 build.jobs 控制。

### 执行性能

独立 replay 使用 AFX 调度器及可见设备。本地 issue wait 最多每 30 秒查询一次。独立客户端的生命周期和资源由 AFX 管理，不持有 AFXX 的环境锁。

### 内存和产物大小

远端任务复制同一份明确列举的脚本集合及 package_sources.json，不依赖本地仓库路径；仅增加小型脚本文件，不改变源码快照、AF run 包或测试数据库布局。

源码快照在同步确认后清理。CANN 包安装成功后清理。AF run 包、任务记录及结果持续保留；wheel 和锁按哈希共享。常驻客户端与 AFX 复测子进程占用独立内存，日志和结果需按使用规模归档。

## 接口设计

### 新增/修改接口描述

主入口命令包括 init、update、deploy、replay、check、status、doctor、issue <操作>。stdout 为 schema_version=1 的 JSON envelope，退出码为 0 或 2。异步任务查询 state，平台查询 terminal/outcome；完整字段及错误码见 [AGENT_USAGE](AGENT_USAGE.md)。

deploy 可关联 issue 并续租，安装完成后返回部署状态。issue verify 在远端使用已冻结配置和候选身份提交，重试复用 platform.json 中的请求。issue wait 使用 round_no 定位验证及其后续回归，不混入其他修复轮次。

### 接口检查项

| 检查项 | 子检查项 | 是否涉及 | 说明 |
|---|---|---|---|
| 接口说明 | 接口评审 | 是 | CLI、配置、AFX 协议与输出约定 |
| 接口说明 | 文档 | 是 | README、依赖清单、agent 调用约定 |
| 接口兼容 | 行为 | 是 | 部署、提交验证与查询采用独立入口 |
| 接口兼容 | ABI/API | 脚本 API | C++ ABI 由原项目维护 |
| 接口约束 | 错误反馈 | 是 | 参数、来源、环境、租约及平台状态分别校验 |
| 接口测试 | 独立用例 | 是 | CLI 实际入口及协议夹具 |

## 软件设计

### 关键数据结构

- config.yaml 保存任务有效配置和 environment_lock。
- current.json 保存当前环境清单，独立于 init/update job ID。
- source.json 保存文件清单和摘要；result.json 引用它并记录包及加载证据。
- request.json、launch.json、status.json 记录提交、启动和阶段状态。
- platform.json 保存候选版本、哈希、幂等键和验证回执。

### 关键技术/算法

SHA-256 用于源码、包、加载文件和锁文件一致性校验。源码同步比较托管清单，删除范围由文件归属限制。复跑汇总对预期 seed/backend/shape 集合核验。动态 Shape 数量从 AFX seed 解码，结果通过 AFX CLI 分页及详情查询。平台轮询核对 issue 编号和 round_no，采用平台验收结论。

### 流程设计

环境流程为 CANN 检查、venv 创建、依赖锁解析、同步安装、加载检查、发布当前清单。更新失败使用旧锁恢复并验证包清单。

部署流程为环境校验、源码同步、构建、安装、加载核验、发布部署。安装失败使部署失效。

平台流程为领取、续租、部署、显式 verify、平台调度官方客户端执行、本地查询。AFXX 在 verify 前调用官方 CLI 启动客户端，不停止或重启，不阻止其平台更新行为。

### 对子模块的修改

| 模块 | 职责 |
|---|---|
| afxx/cli.py | CLI 参数、JSON 响应与命令分发 |
| afxx/common.py | 异常、文件读写与哈希、子进程、文件锁及阶段状态 |
| afxx/config.py | YAML 加载、默认值推导、配置与平台参数校验 |
| afxx/source.py | 源码快照、归属清单、同步与清理 |
| afxx/environment/runtime.py | 环境加载、依赖检查、库探测、部署身份与锁路径 |
| afxx/execution.py | deploy/replay 阶段与环境配置冻结 |
| afxx/jobs.py | 本地预检、幂等提交、远端作业协议及 worker 分发 |
| afxx/bundle.py | 远端任务共用的独立运行文件清单 |
| afxx/environment/bootstrap.py | 托管解释器下载、能力预检与已有解释器复用 |
| afxx/environment/python.py | uv 环境创建、包来源、依赖锁、更新与回滚 |
| afxx/environment/cann.py | CANN 检查、选包、下载、安装和清理 |
| afxx/environment/snapshot.py | 现场采集 job 的 Python、CANN 与 AF 身份 |
| afxx/afx/replay.py | seed 契约、当前 AFX CLI 参数、公共结果查询与覆盖判定 |
| afxx/afx/launcher.py | AFX managed init/start/status 适配与旧客户端迁移检查；不自行管理进程或执行 stop |
| afxx/afx/platform.py | issues HTTP 请求、租约、verify 提交和结果跟踪 |

### 错误处理

#### 系统错误

网络、文件、构建和安装错误保留任务现场及阶段。受管理子进程由操作超时与信号处理约束。环境占用检查失败时保留现场。原子状态无法写入时，查询根据进程信息报告 unknown 或失败。

#### 接口错误

配置、路径、包身份及请求参数错误在对应入口拒绝。HTTP 响应只传播校验后的错误代码。verify 超时保留固定请求，可重复提交；平台失败、环境无效和等待超时分别返回错误码。

## 安全检查

按编码红线核对凭据、路径、资源和接口边界。凭据文件内容只在运行端读取，命令使用参数数组，远端命令经 shell 引用，清理限制到本任务拥有的文件。锁、文件和子进程生命周期覆盖异常分支。外部输入在路径、issue 编号、round_no、设备列表和并行参数使用前校验。图改写、ABI 和 runtime 内部接口由编译器组件负责。

## 兼容性检查

配置使用 YAML 和明确字段，默认值由归一化阶段补齐。环境冻结文件保存实际来源和依赖锁。直接 AFX replay 结果通过适配模块读取；平台适配器依赖 AFX 的 issues HTTP 接口及实际 AF 身份采集接口。安装路径及 Python 导入归属通过预检和实际加载检查确认。

## 测试设计

### 测试边界

入口为 CLI、适配器及环境/进程函数，出口为状态、产物和命令调用。网络、编包、安装及 NPU 接口使用夹具。真实平台和设备执行通过 ST 验证。C++ 算法未在本工具内实现，gtest/RDV 使用各组件已有测试范围。

### 测试用例设计

| 测试类别 | 关键测试项 | 测试方法 | 用例类型 |
|---|---|---|---|
| 功能 | 快照、同步、安装、环境恢复、平台提交、轮次跟踪 | 临时仓库与接口夹具 | pytest UT / ST |
| 异常 | 中断重试、包漂移、回滚失败、过期 PID、租约和网络错误 | 故障注入 | pytest UT / ST |
| 性能 | 增量构建、快照和哈希耗时、日志增长 | 目标环境观测 | 手工验证 |
| 兼容性 | YAML、AFX 版本、CANN 来源、依赖锁 | 参数和来源检查 | pytest UT / ST |
| 特性交叉 | Python/C++ 同源、MLIR codegen、部署与复测锁 | 加载检查与设备用例 | ST / E2E |

### 测试命令

```bash
python3 -m pytest tests -q
```

预期所有脚本用例通过。真实集成验证使用 README 中的 init、deploy、issue verify、issue wait 顺序，并检查实际安装身份及平台终态。

## 验收标准

1. 部署可追溯到源码清单、包哈希和实际加载文件。
2. 环境漂移在同步和复跑前被发现，Python 更新失败可恢复或标记 invalid。
3. 源码同步删除范围、快照清理和幂等重试符合文件所有权。
4. 独立 replay 完整校验 CPU/AF-MLIR 及 shape，保留诊断结果。
5. deploy 与 verify 分开，平台请求重试使用固定候选身份和幂等键。
6. AFX 客户端独立常驻；AFXX 仅负责官方命令的启动、存活检查和复用，不实现客户端循环。
7. 本地 agent 区分通过、失败和等待超时，只采用指定验证及关联回归的结果。
8. 文档命令和参数与当前 CLI 一致，操作说明可直接用于目标环境配置。

### 设计文档检查结果

- [x] 跨特性交叉影响覆盖 SuperKernel、Autofuse、交付、绑定及测试场景。
- [x] 编码红线覆盖凭据、路径、资源生命周期及接口边界。
- [x] 测试设计明确 pytest、ST/E2E 与组件级 gtest/RDV 范围。
- [x] 性能评估覆盖快照、增量构建、轮询和持久产物。

## wheel 与 MCP 接口

独立仓库通过 wheel 提供 `afxx` 和 `afxx mcp`。配置位于用户目录，源码路径显式指定；远端 bundle 从安装包取文件，保持原有包结构。MCP 在本地以独立 CLI 子进程执行公开命令，复用 JSON 错误码与任务协议；远端模块不依赖 MCP SDK。环境初始化与更新走异步任务，查询状态与业务操作分开。

验证覆盖配置优先级、模板防覆盖、MCP stdio 握手和成功/失败响应、wheel 资源完整性及脱离工具源码目录的执行。此次只调整工具分发与接口，不修改 SuperKernel、Autofuse pass/codegen、C++ ABI 或设备运行时；无编译期或设备性能影响。既有任务锁、幂等请求和失败现场保留策略不变。
