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
| issue detail / issue tasks / issue regression-preview / execution show | 本地只读查询平台，不要求租约或 NPU 连接 | ok=true 后读取 data；空 tasks/cases 不代表通过 |
| issue summarize | 本地显式发布已检视总结；不创建远端 job | 返回 status=resolved 和 knowledge_id |
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

AFX 官方客户端在 NPU 独立常驻，通过 `afx platform client run --retest` 启动。verify 提交前通过官方 CLI 检查并启动客户端；不屏蔽平台更新行为，不自动停止或重启。需配置 platform.client_token_file。部署、更新仍执行环境占用检查。不要将 Forge 的锁视为独立客户端的并发保护。

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

客户端启动失败返回 PLATFORM_CLIENT_START_FAILED，查看远端 afx-client/client.log。PLATFORM_CLIENT_MISMATCH 表示存活进程的配置或 AF 身份不匹配；PLATFORM_CLIENT_UNTRACKED 表示同一 HOME 存在无法确认启动环境的进程。上述错误均不提交 verify，也不停止已有进程。

## 平台只读证据查询

- `issue detail --issue-id F152`：返回完整 issue 详情，包含 status、租约、rounds 和 events。
- `issue tasks --issue-id F152 --round-no 2`：返回 `{issue_id, round_no, tasks}`；round_no 为实际 verify 或 regress 轮次，范围 1–999999。不要总用 verify 的轮次查询回归失败。
- `issue regression-preview --issue-id F152`：平台当前预测的回归集合，实际执行集以轮次 tasks 为准。
- `execution show --exec-uuid exec-0001`：读取执行详情、环境、Shape 和日志。AFX 的执行 UUID 是 8–64 位字母、数字、下划线或连字符；不是数据库数值 exec_id，也不要求标准 UUID 格式。

四个命令均复用本地 platform.token_file 和统一 JSON/错误码，不创建远端 job，不改变 issue 状态。MCP 使用相同命令路径，例如 `af_forge_run({"command":"issue tasks","arguments":["--issue-id","F152","--round-no","2"]})`。404 返回 PLATFORM_NOT_FOUND；异常成功响应返回 PLATFORM_RESPONSE_INVALID。

保留平台日志的 log_truncated/log_missing 标记；缺失日志不合成为空日志。任务接口不保证包含 exec_uuid，未拿到可核验执行 ID 时，使用已有 claim 复现信息、任务 detail、客户端日志或平台界面继续定位，不猜测 ID。

当前不提供 knowledge 查询命令。修复总结先保留本地并等待人工检视，检视后显式调用 issue summarize；不能因为本地 commit 完成就声称平台已经 resolved。

## 独立总结提交

`issue summarize --issue-id F152 --summary-file /absolute/path/summary.yaml --request-id summary-F152-001` 读取 YAML 知识字段，使用本地平台 token 和领取记录中的租约提交。MCP 命令路径为 `issue summarize`，参数与 CLI 一致。仅在用户明确要求提交已检视总结时调用，不从修复循环自动发布。

本地必需 kind、title、symptom、root_cause、solution 五个非空字符串；未知字段拒绝，平台继续校验字段类型、长度、状态及权限。文件必须是 YAML，不能仅传自由文本 Markdown。body_md 可作为 YAML 多行字段携带完整正文。

记录存于 local.state_dir/issues/<issue-id>/summaries/<request-id摘要>.json，保存固定请求及成功回执，不保存 token。响应不确定时重试原命令；即使平台已变为 resolved，也会复用相同请求，避免重复发布。成功的本地重试直接返回回执。修改总结、租约或平台后重用同一 request-id 返回 REQUEST_CONFLICT；不要通过更换键盲目重试已发布总结。

SUMMARY_INVALID 表示本地文件或字段有误；PLATFORM_WRONG_STATE、PLATFORM_LEASE_LOST、PLATFORM_UNAUTHORIZED 等平台错误保持透传。平台检查失败不视为归档成功，不自动释放租约或修改 issue 状态。

## 证据分页与续读

```bash
af-forge evidence list --config "$CONFIG" --job-id <replay-job-id>
# 清单被截断时，用返回的 next_offset 继续分页（offset 为文件序号）。
af-forge evidence list --config "$CONFIG" --job-id <replay-job-id> --offset <next_offset>
af-forge evidence read --config "$CONFIG" --job-id <replay-job-id> \
  --path <files中的远端绝对路径> --offset 0 --limit 8192
# 读取后续字节时同时传首次返回的 sha256，拒绝拼接已变化的文件。
af-forge evidence read --config "$CONFIG" --job-id <replay-job-id> \
  --path <同一文件路径> --offset <next_offset> --limit 8192 --sha256 <首次返回的sha256>
```

`read` 返回 content、bytes_read、next_offset、eof、sha256；offset/limit 单位为字节，单次上限 65536。文本按 UTF-8 解码，跨字节边界可能出现替代字符，需要时回读相邻字节。清单分页应在任务终态后进行，运行中的新增文件可能改变排序。接口只读本次 job 的日志和结果目录中的诊断文本，拒绝符号链接、任意配置文件和二进制；不需要平台 token，不创建新任务。文件缺失、变化、越界路径等返回 EVIDENCE_UNAVAILABLE；参数范围错误返回 INVALID_ARGUMENT。

MCP 对应 command 为 `evidence list` / `evidence read`，参数与 CLI 相同。不要让 skill 自己拼 SSH 读取命令。

清单中的 failures 来自 AFX 公共 attempt/shape 结果；每个文件的 associations 仅在 AFX diagnostics 明确引用该文件或其编译目录的完整路径时填入 attempt_id、shape_indices 和关联依据（reference_path 保留原始引用，区分文件/目录引用）。否则 association_state=unassociated，不按文件名猜测 kernel/Shape 归属。空 shape_indices 表示只有 attempt 级引用；一个文件可被多个 attempt 引用，不代表独占归属。
