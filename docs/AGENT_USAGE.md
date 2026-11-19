# Agent 调用约定

入口为 `af-forge`。配置按 `--config`、`AF_FORGE_CONFIG`、当前目录 `af-forge-config.yaml`、用户配置目录 `~/.config/af-forge/config.yaml` 的顺序选择（支持 `XDG_CONFIG_HOME`）。配置字段和操作顺序见 [README](../README.md)，本文件约定响应、状态、重试和完成条件。

## 命令与完成条件

| 命令 | 执行位置与返回方式 | 完成条件 |
|---|---|---|
| doctor | 本地校验配置、工具和 npussh 登录 | 检查通过 |
| init / update | 提交远端任务，默认跟随日志；支持 --detach | 远端任务 success，环境清单 ready |
| deploy | 提交远端任务，返回 job ID | status 为 success，deployment.json 有效；配置 platform 时客户端已刷新 |
| replay | 提交远端任务，返回 job ID | status 为 success 且 report.json.passed=true |
| check | 提交远端依赖检查 | 任务 success、依赖检查通过 |
| status | 查询远端 job | 查询成功后继续检查返回的任务 state |
| issue claim / issue preview / issue renew | 本地直接调用平台 HTTPS | 保存领取响应、预览或续租到期时间 |
| issue verify | 通过 npussh 在远端提交平台请求 | 返回 state=submitted，保存 retest_id |
| issue status | 本地查询平台指定 retest | 检查 terminal 和 outcome |
| issue wait | 本地持续查询平台并续租 | terminal=true 且 outcome=passed |
| client start / client stop / client status | 提交远端管理任务并等待完成 | 检查 data.status["result.json"] 中的客户端状态 |

命令之间使用顶层 data.job_id、data.repair_id、data.retest_id，分别传给 --job-id、--repair-id、--retest-id。replay 的 --job-id 指成功的 deploy 任务；旧的 --deployment 路径仍可使用，两种引用互斥。各子命令的 --help 列出接受的参数，传入无关选项返回 INVALID_ARGUMENT。

所有公共命令 stdout 返回一个 JSON 对象，日志走 stderr。--help 返回帮助文本。

```json
{"schema_version":1,"ok":true,"command":"issue preview","data":{"repair_id":"11111111-1111-4111-8111-111111111111","snapshot":{"targets":[]}},"error":null}
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

提交前生成并保存 request-id，允许 1–128 位字母、数字、下划线、点或连字符。init/update/deploy/replay/check 和 client 管理命令支持此参数。省略时自动生成，但若整个响应丢失，将无法从响应取回该值。

任务状态包括 prepared、queued、running、success、failed、unknown。prepared 为 dry-run 本地产物。status 会返回已有的部署、复跑、环境、平台和客户端报告。每 15–30 秒轮询，长编包阶段可延长到 60 秒；阶段变化或到达终态时汇报。unknown 时检查进程和日志。

init/update 默认跟随远端日志，Ctrl+C 停止本地查看，任务仍可通过 status 查询。client 管理命令也等待远端任务完成。

## 幂等与恢复

同一 local.state_dir 中，request-id 绑定首次请求的配置、参数、job ID 和源码清单。相同请求重试复用原任务；配置或参数变化返回 REQUEST_CONFLICT。dry-run 与实际提交使用不同 ID。修改源码后应创建新请求。

本地请求锁保护冻结过程，远端启动锁和 launch.json 保护任务启动。上传中断可以重试同一请求，已确认远端启动后可清理本地快照。远端同步校验完成后清理远端快照。source.json 保存清理状态及源码清单。

保留 requests/、request.json、launch.json、source.json、deployment.json 及部署注册表引用。已确认启动但远端记录丢失时返回 REMOTE_TASK_LOST。清理后的快照无法用于重新上传原任务。首次准备不完整时先排查，再为新任务选择新 ID。

## 平台修复流程

```bash
af-forge issue claim --request-id claim-001
af-forge issue preview --repair-id <repair-id>
af-forge issue renew --repair-id <repair-id>
af-forge deploy --repair-id <repair-id> --request-id deploy-001
af-forge status --job-id <job-id>
af-forge issue verify --repair-id <repair-id> --job-id <job-id>
af-forge issue wait --repair-id <repair-id> --retest-id <retest-id>
```

使用 --issue-id F152 时，claim/preview/renew/deploy/verify 共用同一 issue_id。claim 接受可选的 --request-id，agent 建议预生成并保存它；网络重试复用原 ID。编号领取成功时 stdout 仅返回 issue_id、status、title、backend、case_count、lease_expires_at（Unix 毫秒）及 request_id。完整领取回执保存在本地 repairs/<issue_id>.json，preview 默认返回 issue_id、failure_summary 和 seeds（按顺序去重）；若回执截断，分页读取平台当前有效用例。--full 返回领取时的 snapshot.cases 和 related_knowledge。verify 返回 state=submitted 和 round_no，可重试同一部署以取得原回执。编号问题的轮次在平台详情中查询。

按 issue_key 或自动分配时，claim 必须提供 request-id，可选 --issue-key 指定 24 位十六进制 issue_key。领取返回 data.repair_id（repair UUID）、lease_id、expires 和 snapshot。无任务时 data=null；下一次领取用新 request-id，重试原请求使用原 ID。领取响应保存在本地 repairs/，修复期间根据 expires 提前续租。关联 repair 的 deploy 在远端自动续租。

issue preview 返回冻结 snapshot 中的实际复测目标，不是独立的回归预览。deploy 和 issue verify 分开执行。verify 使用 job 冻结配置，核对部署、实际 AF 及专用 client ID，提交 repairs/{id}/retest。platform.json 保存固定请求及回执，一个部署 job 绑定一个 repair。新修复或新复测提交使用新部署 job。

issue wait 必须传入 verify 返回的 retest_id。只以该 retest 的 terminal/passed 判定，不把 repair 状态或其他轮结果当作通过。修复状态 verified 表示可提交总结，resolved 表示归档完成。当前没有单独的自动 regress 轮次，workflow 不代填或提交修复总结。

超时继续查询同一 retest；PLATFORM_RETEST_SUPERSEDED 表示平台已切换到其他复测，当前详情 API 无法查询指定旧 retest。再次部署前等待当前复测结束，避免定向客户端运行期间环境变化。

## 专用客户端

`client start/client stop/client status` 使用远端管理任务。client status 的任务 success 表示查询完成，客户端自身可能为 stopped、starting、ready、draining、unhealthy 或 failed。检查 result.json 的状态、日志路径和安装身份。

deploy 停止受管理客户端后才开始安装流程。停止超时表示客户端可能仍在完成复测，待其结束后重试。环境 update 前停止客户端，成功后重新启动。客户端采用配置的 venv/CANN 和 replay.devices，包更新统一交给 workflow update。

本地和远端使用同一平台用户的 Bearer token；远端凭据文件权限为 0600。凭据内容只在使用端读取，配置和命令参数仅传路径。

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
| PLATFORM_CONFLICT / PLATFORM_UNAUTHORIZED | 核对 repair 租约、请求键、用户及客户端归属 |
| PLATFORM_VERIFY_FAILED | 检查远端提交错误、客户端身份和已保存请求 |
| PLATFORM_VERIFICATION_FAILED | 从 data.retest 查看复测状态、attempt 和 artifact |
| PLATFORM_RETEST_SUPERSEDED | 不使用其他 retest 的结果；检查保存的提交回执 |
| PLATFORM_WAIT_TIMEOUT | 继续查询同一 repair 和 retest |
| INTERNAL_ERROR | 保留现场并排查实现错误 |

issue verify 将远端已分类错误的 code 和 message 原样返回到 error.code 和 error.message。未分类异常及仅含错误文本的旧任务回执使用 PLATFORM_VERIFY_FAILED，并保留错误文本。SSH 连接或响应读取失败使用 REMOTE_UNAVAILABLE。

平台其他 HTTP 错误返回 PLATFORM_ 前缀的服务端错误代码。远端任务按阶段保存 error_code，包括 SOURCE_SYNC_FAILED、BUILD_FAILED、INSTALL_FAILED、INSTALL_VERIFY_FAILED、REMOTE_DEPENDENCY_FAILED、REPLAY_PREPARE_FAILED、REPLAY_FAILED、CANN_PREPARE_FAILED、CANN_INSTALL_FAILED、PLATFORM_CLIENT_FAILED 和 WORKER_FAILED。LAUNCH_UNCERTAIN 表示启动状态需要检查。

Python 更新失败时同时读取 result.json.error 和 rollback，分别确认原始错误与恢复结果。AF 安装失败会使部署注册表失效。平台提交错误通过独立 issue verify 重试，部署记录继续保留。

环境一致性差异以 WARNING 写入 job.log，不作为 deploy/replay 的失败条件。发生差异时 job 配置保留提交值，不附加旧环境锁；不要将其视为实际环境的精确快照。运行依赖检查和 AF 部署产物完整性检查仍需通过。
