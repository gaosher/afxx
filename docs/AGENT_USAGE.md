# Agent 调用约定

入口为 `af-forge`。配置按 `--config`、`AF_FORGE_CONFIG`、当前目录 `af-forge-config.yaml`、用户配置目录 `~/.config/af-forge/config.yaml` 的顺序选择（支持 `XDG_CONFIG_HOME`）。配置字段和操作顺序见 [README](../README.md)，本文件约定响应、状态、重试和完成条件。

## 命令与完成条件

| 命令 | 执行位置与返回方式 | 完成条件 |
|---|---|---|
| doctor | 本地校验配置、工具和 npussh 登录 | 检查通过 |
| init / update | 提交远端任务，默认跟随日志；支持 --detach | 远端任务 success，环境清单 ready |
| deploy | 提交远端任务，返回 job ID | status 为 success，deployment.json 有效 |
| replay | 提交远端任务，返回 job ID | status 为 success 且 report.json.passed=true |
| check | 提交远端依赖检查 | 任务 success、依赖检查通过 |
| status | 查询远端 job | 查询成功后继续检查返回的任务 state |
| issue claim / issue preview / issue renew | 本地直接调用平台 HTTPS | 保存领取响应、预览或续租到期时间 |
| issue verify | 通过 npussh 在远端提交平台请求 | 返回 state=submitted，保存 round_no |
| issue status | 本地查询平台指定验证轮次 | 检查 terminal 和 outcome |
| issue wait | 本地持续查询平台并续租 | terminal=true 且 outcome=passed |

命令之间使用 data.job_id、data.issue_id、data.round_no，分别传给 --job-id、--issue-id、--round-no。replay 使用成功 deploy 的 --job-id。各命令的 --help 列出接受的参数。

所有公共命令 stdout 返回一个 JSON 对象，日志走 stderr。--help 返回帮助文本。

```json
{"schema_version":1,"ok":true,"command":"issue preview","data":{"issue_id":"F152","snapshot":{"targets":[]}},"error":null}
```

```json
{"schema_version":1,"ok":false,"command":"doctor","data":null,"error":{"code":"AUTH_REQUIRED","message":"请执行 npussh login"}}
```

退出码 0 表示调用成功，2 表示请求失败。异步提交成功后继续查询远端任务。init/update 的最终响应为摘要，详细检查报告通过 status 获取。平台等待失败或超时时，data 保留查询结果。

## 部署和任务轮询

```bash
af-forge deploy --request-id deploy-001
af-forge status --job-id <job-id>
```

提交前生成并保存 request-id，允许 1–128 位字母、数字、下划线、点或连字符。init/update/deploy/replay/check支持此参数。省略时自动生成，但若整个响应丢失，将无法从响应取回该值。

任务状态包括 prepared、queued、running、success、failed、unknown。prepared 为 dry-run 本地产物。status 会返回已有的部署、复跑、环境、平台报告。每 15–30 秒轮询，长编包阶段可延长到 60 秒；阶段变化或到达终态时汇报。unknown 时检查进程和日志。

init/update 默认跟随远端日志，Ctrl+C 停止本地查看，任务仍可通过 status 查询。

## 幂等与恢复

同一 local.state_dir 中，request-id 绑定首次请求的配置、参数、job ID 和源码清单。相同请求重试复用原任务；配置或参数变化返回 REQUEST_CONFLICT。dry-run 与实际提交使用不同 ID。修改源码后应创建新请求。

本地请求锁保护冻结过程，远端启动锁和 launch.json 保护任务启动。上传中断可以重试同一请求，已确认远端启动后可清理本地快照。远端同步校验完成后清理远端快照。source.json 保存清理状态及源码清单。

保留 requests/、request.json、launch.json、source.json、deployment.json 及部署注册表引用。已确认启动但远端记录丢失时返回 REMOTE_TASK_LOST。清理后的快照无法用于重新上传原任务。首次准备不完整时先排查，再为新任务选择新 ID。

## 平台修复流程

```bash
af-forge issue claim --request-id claim-001
af-forge issue preview --issue-id F152
af-forge issue renew --issue-id F152
af-forge deploy --issue-id F152 --request-id deploy-001
af-forge status --job-id <job-id>
af-forge issue verify --issue-id F152 --job-id <job-id>
af-forge issue wait --issue-id F152 --round-no <round_no>
```

claim 可用 --issue-id 指定问题；省略时由平台分配。领取回执存于 issues/<issue_id>.json，包含复现信息和租约。重复请求复用 request-id；无任务时返回 null。preview 默认输出失败摘要及去重 seed，--full 返回完整复现信息。

verify 与 deploy 分离，在远端采集实际 AF 身份，核对部署后提交 `/api/issues/{id}/verify`。platform.json 固定候选身份、租约和幂等键；已提交时返回原回执。一个部署只绑定一个问题和租约。

status/wait 以 verify 返回的 round_no 定位验证，跟踪同一候选包的下一轮 regress。只有相关轮次全部通过才返回 outcome=passed。wait 遇到失败和 invalid 均返回 PLATFORM_VERIFICATION_FAILED；等待超时可继续查同一轮次。查询保留历史轮次身份，不使用其他修复的结果。

AFX 官方客户端在 NPU 独立常驻，通过 `afx platform client run --retest` 启动。Forge 不管理它的进程或启停，也不屏蔽平台更新行为。部署、更新仍执行环境占用检查。不要将 Forge 的锁视为独立客户端的并发保护。

## 错误处理

自动分支依据 error.code，message 用于展示。

| 错误码 | 处理 |
|---|---|
| INVALID_ARGUMENT / CONFIG_INVALID | 修正参数或配置；新任务使用新 ID |
| DEPENDENCY_MISSING / DEPENDENCY_UNUSABLE | 修复启动解释器或本地工具 |
| AUTH_REQUIRED | 执行 npussh login |
| AUTH_CHECK_TIMEOUT / AUTH_SERVICE_UNAVAILABLE | 检查 npussh list 和连接服务 |
| REQUEST_CONFLICT | 查询原任务，为不同请求使用新 ID |
| PREPARATION_FAILED / PREPARATION_INCOMPLETE | 检查本地任务现场 |
| TRANSFER_FAILED / REMOTE_UNAVAILABLE | 保存 job/request-id，重试同一请求或查询原任务 |
| REMOTE_TASK_LOST / SNAPSHOT_UNAVAILABLE | 排查记录和快照缺失 |
| JOB_NOT_FOUND | 检查目标环境、任务目录和 ID |
| ENVIRONMENT_MISMATCH | 核对配置、current.json 和实际环境 |
| PLATFORM_CREDENTIAL_MISSING | 检查相应端的凭据文件 |
| PLATFORM_UNAVAILABLE / PLATFORM_RESPONSE_INVALID | 检查平台连接和响应，重试原请求 |
| PLATFORM_CONFLICT / PLATFORM_UNAUTHORIZED | 核对 issue 租约、请求键、用户及客户端归属 |
| PLATFORM_VERIFY_FAILED | 检查远端提交错误、包身份和已保存请求 |
| PLATFORM_VERIFICATION_FAILED | 查看 data.verification 和 data.regression |
| PLATFORM_WAIT_TIMEOUT | 继续查询同一 issue 和 round_no |
| INTERNAL_ERROR | 保留现场并排查实现错误 |

issue verify 将远端已分类错误的 code 和 message 原样返回到 error.code 和 error.message。未分类异常及仅含错误文本的旧任务回执使用 PLATFORM_VERIFY_FAILED，并保留错误文本。SSH 连接或响应读取失败使用 REMOTE_UNAVAILABLE。

平台其他 HTTP 错误返回 PLATFORM_ 前缀的服务端错误代码。远端任务按阶段保存 error_code，包括 SOURCE_SYNC_FAILED、BUILD_FAILED、INSTALL_FAILED、INSTALL_VERIFY_FAILED、REMOTE_DEPENDENCY_FAILED、REPLAY_PREPARE_FAILED、REPLAY_FAILED、CANN_PREPARE_FAILED、CANN_INSTALL_FAILED、PLATFORM_CLIENT_FAILED 和 WORKER_FAILED。LAUNCH_UNCERTAIN 表示启动状态需要检查。

Python 更新失败时同时读取 result.json.error 和 rollback，分别确认原始错误与恢复结果。AF 安装失败会使部署注册表失效。平台提交错误通过独立 issue verify 重试，部署记录继续保留。

环境一致性差异以 WARNING 写入 job.log，不作为 deploy/replay 的失败条件。发生差异时 job 配置保留提交值，不附加旧环境锁；不要将其视为实际环境的精确快照。运行依赖检查和 AF 部署产物完整性检查仍需通过。
