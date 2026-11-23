# Agent 调用约定

所有命令（包括 env init/update）默认读取 `~/.config/afxx/config.yaml`，仅显式 `--config` 覆盖。不读取 AFXX_CONFIG、XDG_CONFIG_HOME 或工作目录配置。`config init --source <源码目录>` 在默认位置生成配置；`--output` 可指定其他位置，已有文件不会覆盖。

MCP 优先使用结构化工具。长操作先选定 request_id，再提交并用 afxx_operation_status 查询；completed 后读取 data.result 中的原 CLI 回执，随后按下表追踪 job 或验证轮次。MCP 取消/断开不停止已提交的长操作；unknown 不得视为成功或直接换键重试。工具 config 与 arguments 内 --config 冲突时不执行。详见 [MCP 调用](afxx-commands.md#11-mcp-调用)。

## 命令与完成条件

| 命令 | 执行位置与返回方式 | 完成条件 |
|---|---|---|
| doctor | 本地校验配置、工具和 npussh 登录 | 检查通过 |
| init / update | 提交远端任务，默认跟随日志；支持 --detach | 远端任务 success，环境清单 ready |
| deploy | 提交远端任务，返回 job ID | status 为 success，result.json 有效 |
| fuzz run | 远端 job 调用官方 AFX，返回 job ID | status 为 success 仅代表进程正常结束；用 fuzz status 查询用例结果 |
| fuzz status / fuzz stop | 调用该 job 的 AFX 官方命令 | status 返回队列信息；stop 仅确认停止请求，随后轮询任务终态 |
| evidence list / evidence read | 本地通过 npussh 只读远端证据 | ok=true；按 next_offset 续读，检查 sha256 和未关联标记 |
| replay | 提交远端任务，返回 job ID | data.state=success 且 data.replay.passed=true |
| check | 提交远端依赖检查 | 任务 success、依赖检查通过 |
| status | 查询远端 job | 查询成功后继续检查返回的任务 state |
| issue claim / issue preview / issue renew | 本地直接调用平台 HTTPS | 保存领取响应、预览或续租到期时间 |
| issue detail / issue tasks / issue regression-preview / execution show | 本地只读查询平台，不要求租约或 NPU 连接 | ok=true 后读取 data；空 tasks/cases 不代表通过 |
| issue submit-summary | 本地显式发布已检视总结；不创建远端 job | 返回 status=resolved 和 knowledge_id |
| issue verify | 通过 npussh 在远端提交平台请求 | 返回 state=submitted，保存 round_no |
| issue status | 本地查询平台指定验证轮次 | 检查 terminal 和 outcome |
| issue wait | 本地持续查询平台并续租 | terminal=true 且 outcome=passed |

fuzz 使用固定的 `<workflow_root>/fuzz/`，一次只允许一个运行。重复启动的任务报 `FUZZ_ALREADY_RUNNING`；`FUZZ_JOB_REPLACED` 表示该目录已由新 job 接管，应使用新 job ID 控制。历史记录仍用普通 status 查询。

命令之间使用 data.job_id、data.issue_id、data.round_no，分别传给 --job-id、--issue-id、--round-no。replay 使用成功 deploy 的 --job-id。各命令的 --help 列出接受的参数。

所有公共命令 stdout 返回一个 JSON 对象，日志走 stderr。--help 返回帮助文本。

```json
{"schema_version":1,"ok":true,"command":"issue preview","data":{"issue_id":"F152","snapshot":{"targets":[]}},"error":null}
```

```json
{"schema_version":1,"ok":false,"command":"doctor","data":null,"error":{"code":"AUTH_REQUIRED","message":"请执行 npussh login"}}
```

退出码 0 表示调用成功，2 表示请求失败。异步提交成功后继续查询远端任务。init/update 的最终响应为摘要，详细检查报告保存在远端任务目录。平台等待失败时 data 保留终态详情；等待超时时仅保留 issue_id、round_no 和 event=timeout。

## 部署和任务轮询

```bash
afxx deploy --request-id deploy-001
afxx status --job-id <job-id>
```

提交前生成并保存 request-id，允许 1–128 位字母、数字、下划线、点或连字符。init/update/deploy/replay/check/fuzz run 支持此参数。省略时自动生成，但若整个响应丢失，将无法从响应取回该值。

任务状态包括 prepared、queued、running、success、failed、unknown。prepared 为 dry-run 本地产物。非 replay 终态的 status 返回概要；replay 终态自动下载全量证据，单个执行使用 status --attempt --fields。等待任务使用 status --wait --since，避免重复概要进入上下文；阶段变化或到达终态时汇报。unknown 时检查进程和日志。

init/update 默认跟随远端日志，Ctrl+C 停止本地查看，任务仍可通过 status 查询。

## 幂等与恢复

同一 local.state_dir 中，request-id 绑定首次请求的配置、参数、job ID 和源码清单。相同请求重试复用原任务；配置或参数变化返回 REQUEST_CONFLICT。dry-run 与实际提交使用不同 ID。修改源码后应创建新请求。

本地请求锁保护冻结过程，远端启动锁和 launch.json 保护任务启动。上传中断可以重试同一请求，已确认远端启动后可清理本地快照。远端同步校验完成后清理远端快照。source.json 保存清理状态及源码清单。

保留 requests/、request.json、launch.json、source.json、result.json 及部署注册表引用。已确认启动但远端记录丢失时返回 REMOTE_TASK_LOST。清理后的快照无法用于重新上传原任务。首次准备不完整时先排查，再为新任务选择新 ID。

## 平台修复流程

```bash
afxx issue claim --issue-id F152 --request-id claim-001
afxx issue preview --issue-id F152
afxx issue renew --issue-id F152
afxx deploy --issue-id F152 --request-id deploy-001
afxx status --job-id <job-id>
afxx issue verify --issue-id F152 --job-id <job-id>
afxx issue verify-check --issue-id F152 --round-no <round_no> --wait --timeout 30
```

claim 必须用 --issue-id 指定问题。领取回执存于 issues/<issue_id>.json，包含复现信息和租约。重复请求复用 request-id；无任务时返回 null。preview 默认输出失败摘要及去重 seed，--full 返回完整复现信息。

verify 与 deploy 分离，在远端采集实际 AF 身份，核对部署后提交 `/api/issues/{id}/verify`。platform.json 固定候选身份、租约和幂等键；已提交时返回原回执。一个部署只绑定一个问题和租约。

status/wait 以 verify 返回的 round_no 定位验证，跟踪同一候选包的下一轮 regress。只有相关轮次全部通过才返回 outcome=passed。wait 遇到失败和 invalid 均返回 PLATFORM_VERIFICATION_FAILED；等待超时可继续查同一轮次。查询保留历史轮次身份，不使用其他修复的结果。

AFX >= 0.2.3 managed client 在 NPU 常驻。verify 前由官方 init/start/status 初始化、启动并确认执行会话运行，需配置 platform.client_token_file。start 会读取平台配置，可能开始 fuzz。AFXX 不自动 stop/restart；环境变化需要刷新进程时使用同一环境、同一 data-dir 的 afx restart。replay 保留独立入口，不享有 managed 队列调度或抢占优先级。部署、更新仍执行环境占用检查。

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
| ENVIRONMENT_SNAPSHOT_FAILED | 检查环境快照是否可写入 job 目录 |
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

deploy/replay/check 保留提交配置，并将现场采集的 Python 包版本与来源、CANN 版本和 AF 加载身份写入 environment.json；缺失信息见 errors。日常任务不比对历史环境清单或检查缓存 wheel。运行依赖检查和 AF 部署产物完整性检查仍需通过。

客户端启动失败返回 PLATFORM_CLIENT_START_FAILED，检查远端 afx-client/client.log 和 runs/<run-id>/execution.log。PLATFORM_CLIENT_MISMATCH 表示 managed 配置的 endpoint、token 或设备不匹配；PLATFORM_CLIENT_UNTRACKED 表示旧客户端仍存活，需要先停止旧进程再迁移。上述错误均不提交 verify，也不自动停止已有进程。

## 平台只读证据查询

- `issue detail --issue-id F152`：返回完整 issue 详情，包含 status、租约、rounds 和 events。
- `issue tasks --issue-id F152 --round-no 2`：返回 `{issue_id, round_no, tasks}`；round_no 为实际 verify 或 regress 轮次，范围 1–999999。不要总用 verify 的轮次查询回归失败。
- `issue regression-preview --issue-id F152`：平台当前预测的回归集合，实际执行集以轮次 tasks 为准。
- `execution show --exec-uuid exec-0001`：读取执行详情、环境、Shape 和日志。AFX 的执行 UUID 是 8–64 位字母、数字、下划线或连字符；不是数据库数值 exec_id，也不要求标准 UUID 格式。

四个命令均复用本地 platform.token_file 和统一 JSON/错误码，不创建远端 job，不改变 issue 状态。MCP 使用相同命令路径，例如 `afxx_run({"command":"issue tasks","arguments":["--issue-id","F152","--round-no","2"]})`。404 返回 PLATFORM_NOT_FOUND；异常成功响应返回 PLATFORM_RESPONSE_INVALID。

保留平台日志的 log_truncated/log_missing 标记；缺失日志不合成为空日志。任务接口不保证包含 exec_uuid，未拿到可核验执行 ID 时，使用已有 claim 复现信息、任务 detail、客户端日志或平台界面继续定位，不猜测 ID。

当前不提供 knowledge 查询命令。修复总结先保留本地并等待人工检视，检视后显式调用 issue submit-summary；不能因为本地 commit 完成就声称平台已经 resolved。

## 独立总结提交

`issue submit-summary --issue-id F152 --summary-file /absolute/path/summary.yaml --request-id summary-F152-001` 读取 YAML 知识字段，使用本地平台 token 和领取记录中的租约提交。MCP 命令路径为 `issue submit-summary`，参数与 CLI 一致。仅在用户明确要求提交已检视总结时调用，不从修复循环自动发布。

本地必需 kind、title、symptom、root_cause、solution 五个非空字符串；未知字段拒绝，平台继续校验字段类型、长度、状态及权限。文件必须是 YAML，不能仅传自由文本 Markdown。body_md 可作为 YAML 多行字段携带完整正文。 `afx-issue-repair` 完成修复后交给独立的 [afx-issue-summary](../skills/afx-issue-summary/SKILL.md)，后者使用 [summary.yaml 模板](../skills/afx-issue-summary/assets/summary.yaml)，在 body_md 中保留完整证据链，供人工检视及后续知识检索；这是修复交付约定，不改变平台字段必填规则。

总结正文以技术分析为主：按独立根因展示最小实际错误 IR（无 IR 时使用实际日志、代码或输入输出），结合源码符号解释错误机制、修复行为和验证依据。错误机制应追溯关键类型、属性或操作的上游来源、转换过程与违反约束的位置。修复分析说明语义为何保持；替换操作、放宽判断或新增分支时，解释选择依据、适用边界、后续处理支持及直接替代方案的取舍。验证证据应区分编译通过、输出正确和边界行为保持，并对应修复依赖的语义假设；未证明的部分留作人工检视问题。片段附一行具体来源；修复后 IR 必须来自实际产物，没有时说明转换规则，不能伪造现场。顶层 symptom/root_cause/solution 保持简短，正文不重复概述。

复现条件只保留必要输入与基线 SHA。部署/request/attempt ID、环境目录、包哈希和构建过程留在任务记录，除非直接解释根因。提交前仍需核对已验证源码与 fix_ref 一致，但无需把核对流水账写进总结。每条根因的“验证依据”记录测试覆盖的语义与实际结果，必要时对照同输入修复前后行为，影响结论的限制在该处说明。不单列验证汇总章节；正文末尾保留一行平台地址、issue ID、verify/关联 regression 轮次与结果。无回归候选、skip 和未运行不计通过。

人工检视部分列具体代码语义及受影响分支；同类问题定位部分写识别信号、排查方法、预期观察和适用边界。总结应独立可读，正文包含理解结论所需的实际片段与分析，不包含本地或远端机器的绝对路径。源码与测试使用仓库相对路径和符号名，由 fix_ref 固定版本；外部引用仅保留可共享的平台记录或归档附件。没有可共享引用时说明产物类型（如“基线 AFIR dump”），原始路径留在任务记录；缺失证据明确说明，不自动上传附件。

记录存于 local.state_dir/issues/<issue-id>/summaries/<request-id摘要>.json，保存固定请求及成功回执，不保存 token。响应不确定时重试原命令；即使平台已变为 resolved，也会复用相同请求，避免重复发布。成功的本地重试直接返回回执。修改总结、租约或平台后重用同一 request-id 返回 REQUEST_CONFLICT；不要通过更换键盲目重试已发布总结。

SUMMARY_INVALID 表示本地文件或字段有误；PLATFORM_WRONG_STATE、PLATFORM_LEASE_LOST、PLATFORM_UNAUTHORIZED 等平台错误保持透传。平台检查失败不视为归档成功，不自动释放租约或修改 issue 状态。

## 证据分页与续读

```bash
afxx evidence list --config "$CONFIG" --job-id <replay-job-id>
# 清单被截断时，用返回的 next_offset 继续分页（offset 为文件序号）。
afxx evidence list --config "$CONFIG" --job-id <replay-job-id> --offset <next_offset>
afxx evidence read --config "$CONFIG" --job-id <replay-job-id> \
  --path <files中的远端绝对路径> --offset 0 --limit 8192
# 读取后续字节时同时传首次返回的 sha256，拒绝拼接已变化的文件。
afxx evidence read --config "$CONFIG" --job-id <replay-job-id> \
  --path <同一文件路径> --offset <next_offset> --limit 8192 --sha256 <首次返回的sha256>
```

`read` 返回 content、bytes_read、next_offset、eof、sha256；offset/limit 单位为字节，单次上限 65536。文本按 UTF-8 解码，跨字节边界可能出现替代字符，需要时回读相邻字节。清单分页应在任务终态后进行，运行中的新增文件可能改变排序。接口只读本次 job 的日志和结果目录中的诊断文本，拒绝符号链接、任意配置文件和二进制；不需要平台 token，不创建新任务。文件缺失、变化、越界路径等返回 EVIDENCE_UNAVAILABLE；参数范围错误返回 INVALID_ARGUMENT。

MCP 对应 command 为 `evidence list` / `evidence read`，参数与 CLI 相同。不要让 skill 自己拼 SSH 读取命令。

清单中的 failures 来自 AFX 公共 attempt/shape 结果；每个文件的 associations 仅在 AFX diagnostics 明确引用该文件或其编译目录的完整路径时填入 attempt_id、shape_indices 和关联依据（reference_path 保留原始引用，区分文件/目录引用）。否则 association_state=unassociated，不按文件名猜测 kernel/Shape 归属。空 shape_indices 表示只有 attempt 级引用；一个文件可被多个 attempt 引用，不代表独占归属。

## Status 概要、字段查询与等待

```bash
# 运行中返回概要和 cursor；replay 终态自动下载全量现场。
afxx status --config "$CONFIG" --job-id <job-id>
# 默认 attempt 只返回简短身份、状态、错误摘要；详细字段需显式选择。
afxx status --config "$CONFIG" --job-id <replay-job-id> --attempt <attempt-id>
afxx status --config "$CONFIG" --job-id <replay-job-id> --attempt <attempt-id> \
  --fields error_stage,error_summary,diagnostic_view.summary,shape_results
# 独立读取环境或图，避免把整份 attempt 放入上下文。
afxx status --config "$CONFIG" --job-id <replay-job-id> --attempt <attempt-id> \
  --fields contexts.environment
# 只等待变化/终态；没有变化时返回小型 timeout 回执。
afxx status --config "$CONFIG" --job-id <job-id> --wait --since <cursor> --timeout 30
```

replay 到达 success/failed 后，本地 `status` 或 `status --wait` 自动将完整 dump、日志、报告、结果数据库和清单下载到 `/tmp/afxx-evidence-<uid>/<job-id>-<环境标识>/`。返回仅包含 job_id、state、evidence_path 和 failures（attempt_id、seed、backend、shape_indices、stage、简短 error）；失败超过 20 项时附数量和截断标记。异步提交后仍需调用 status/wait，远端不会主动推送文件。重复查询校验并复用本地文件，不重复下载。

非 replay 终态仍返回概要和 cursor；`--attempt` 与 `--wait` 互斥。完整本地文件布局、缓存和失败重试规则见 [复现证据清单](../skills/afx-issue-repair/references/replay-evidence.md#replay-自动返回的证据)。按需提取文件内容，不要整体读取到上下文。

attempt 默认字段为 id、seed_short、backend、status、error_stage、error_summary（缺失的可选字段省略），字符串最多 512 字符，裁剪字段列于 attempt_truncated_fields。`--fields` 使用逗号分隔的字段或字典点路径，例如 metadata.graph、contexts.environment、diagnostic_view.summary、shape_results；选择结果以完整字段路径为键。显式选择的大字段会完整返回，按定位需要逐次读取。未知执行返回 ATTEMPT_NOT_FOUND，未知字段返回 ATTEMPT_FIELD_NOT_FOUND，不默默忽略拼写错误。

wait 默认 30 秒、允许 (0,50] 秒，CLI/MCP 均返回一次 JSON。携带 since 时与上次概要比较；未传时以首次查询为基线等待下一次变化。event=changed/terminal 返回新概要和 cursor；replay 终态直接返回上述 evidence_path 和 failures，不附 event/cursor；state=unknown 返回 event=attention，需处理进程不确定状态。没有变化时只返回 job_id、event=timeout、cursor，表示等待窗口结束而非任务失败。下一次继续使用返回的 cursor；不重复提交原任务。网络请求本身的超时独立于等待窗口。

公开任务 ID 统一为 job_id，提交结果不重复返回 job 别名或嵌套 status。state 与 passed 含义不同；外层 ok 只表示查询成功。概要和字段投影在远端完成，避免轮询传回大图/日志。replay 终态优先读取本地证据文件；evidence list/read 保留为按需远端查询入口。

### Job 记录入口

通过 status 获取摘要，使用 status --full 获取 result.json、environment.json 和 platform.json 详情。result.json 按任务类型保存部署身份、replay 结果或环境更新/回滚结果；CANN 安装过程位于 result.json 的 cann 字段。Python 依赖检查位于 environment.json 的 dependencies 字段。请求、启动及状态继续独立保存，以保持不同进程的写入边界。

使用 `client status/start/stop/restart` 管理远端唯一 client，CLI/MCP 行为一致。部署前先查状态，需要暂停或刷新时在已有授权范围内调用 stop/restart；不要通过 SSH 拼接 kill 或启动第二个 client。stop 超时先查 status，不把请求已接受视为退出完成。start 可能启动 fuzz，平台派发问题仍待定位；保留操作前状态，按用户约定恢复。详见 [生命周期接口](afxx-commands.md#client-生命周期)。

平台定位补充入口：`case spec --case-key <seed>` 读取图元数据，`case history --case-key <seed>` 分页读取执行历史，再用 `execution show` 读取具体执行日志。`retest list` 查询当前用户手动复测批次（不含 issue verify；后者使用 `issue status/tasks`）。这些都是本机 HTTP 查询，不需要启动远端 client。

用户明确要求退出已领取的 issue 时，使用 `issue release --issue-id <id> --request-id <稳定请求键>`；失败重试沿用该键，成功后本地租约不可再用，重新领取使用新的请求键。释放不等于取消已提交的执行。对应 MCP：`afxx_issue(action="release")`、`afxx_case(action="spec")`、`afxx_case(action="history")`、`afxx_run(command="retest list")`，参数与 CLI 对齐，均支持可选 `config`。

定位时优先使用结构化 MCP `afxx_issue_status(issue_id, round_no, include_tasks=True)` 找到失败执行，再用 `afxx_execution_show(exec_uuid)` 读取平台详情。远端 dump 使用 `afxx_evidence_list(job_id, offset)` 和 `afxx_evidence_read(job_id, path, offset, limit, sha256)`；list offset 为文件游标，read offset/limit 为字节。工具直接返回 CLI 结果及错误，无须查询 operation_status。所有工具支持可选 config，且保留 afxx_run 通用调用方式。

验证闭环优先使用 issue verify-check，MCP 为 afxx_issue(action="verify-check", issue_id=..., round_no=..., wait=true, timeout=30)。直接返回聚合结果，无 operation 回执；timed_out 不代表任务失败，继续查询原轮次。该命令不续租、不重启、不提交，不将 skip/invalid 算通过。claim 现在 CLI/MCP 均必须指定 issue_id。
