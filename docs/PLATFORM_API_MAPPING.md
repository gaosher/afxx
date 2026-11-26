# CLI / MCP / 平台 OpenAPI 实现对照

本文记录 **AFXX 当前源码实现**，不代表已发布 wheel 或远端安装版本。命令一律省略 `afxx` 前缀。

- 核对日期：2026-11-23。
- 上游：[AutofuseX Platform OpenAPI](https://autofuse.online/openapi.json)，OpenAPI 3.1.0，API 版本 1。
- 本次快照：60 个路径、65 个 HTTP 操作；SHA-256：`a875c46a1e2d14726994fe81f55089b9d261729683d7cccbf1dfad773a4daa08`。
- 实现依据：[CLI](../afxx/cli.py)、[MCP](../afxx/mcp_server.py)、[平台适配](../afxx/afx/platform.py)；参数和调用例子见[命令参考](afxx-commands.md)。

## 平台接口全表

下面逐项列出 65 个操作，不把 AFXX 聚合命令重复计为新增 API 覆盖。

当前覆盖：独立封装 **17** 项、内部部分覆盖 **0** 项、AFX client 负责 **5** 项、未封装 **43** 项。

`GET /api/clients` 保留在上游接口清单中，但 AFXX 的 CLI、通用 MCP 命令和结构化 MCP 工具均已移除；不列入后续补齐计划。远端 `client status/start/stop/restart` 不受影响。

| 方法与路径 | API 名称（summary） | 状态 | 当前 CLI | 当前 MCP | 范围与限制 |
| --- | --- | --- | --- | --- | --- |
| `GET /api/me` | Me | 已封装 | `platform me` | 仅通过 `afxx_run` | 查询 token 对应的 user_id、role。 |
| `GET /api/cases` | Cases | 已封装 | `case list` | 仅通过 `afxx_run` | 筛选或精确查询、整数游标分页，返回 groups 和 next_cursor。 |
| `GET /api/cases/{case_key}/history` | Case History | 已封装 | `case history` | `afxx_case(action="history")` + `afxx_run` | 分页执行历史，保留 head/stale_graph。 |
| `GET /api/cases/{case_key}/spec` | Case Spec | 已封装 | `case spec` | `afxx_case(action="spec")` + `afxx_run` | 仅图元数据；空对象表示无已存 spec。 |
| `GET /api/executions/{exec_uuid}` | Execution | 已封装 | `case execution` | `afxx_case_execution` + `afxx_run` | 结构化参数 exec_uuid；平台详情与日志，保留缺失/截断标记，无需 NPU。 |
| `GET /api/clients` | Clients | 未封装 | — | — | 全平台客户端注册列表；当前修复工作流不需要，已移除封装。 |
| `GET /api/settings` | Settings | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `GET /api/search/fields` | Search Fields | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `GET /api/issues` | Issue List | 已封装 | `issue list` | 仅通过 `afxx_run` | 筛选、字符串游标分页，limit 为 1–200。 |
| `POST /api/issues/claim` | Claim | 已封装 | `issue claim` | `afxx_issue(action="claim")` + `afxx_run` | CLI 与结构化 MCP 均必填 issue_id，保存本地租约。 |
| `GET /api/issues/{issue_id}` | Issue Detail | 已封装 | `issue detail` | 仅通过 `afxx_run` | 实时详情；另被 status/wait 聚合使用。 |
| `GET /api/issues/{issue_id}/cases` | Issue Cases | 已封装 | `issue cases` | 仅通过 `afxx_run` | affected/regression、整数游标；不提供 limit；preview 也会内部调用。 |
| `GET /api/issues/{issue_id}/rounds/{round_no}/tasks` | Round Tasks | 已封装 | `issue tasks` | `afxx_issue_status(include_tasks=True)` + `afxx_run` | MCP 经 issue status 聚合验证及关联回归任务；CLI issue tasks 可单独查询指定轮次。 |
| `POST /api/issues/{issue_id}/renew` | Renew | 已封装 | `issue renew` | 仅通过 `afxx_run` | 显式续租；deploy 不参与租约管理。 |
| `POST /api/issues/{issue_id}/release` | Release | 已封装 | `issue release` | `afxx_issue(action="release")` + `afxx_run` | 幂等释放并停用本地租约；不取消执行任务。 |
| `POST /api/issues/{issue_id}/verify` | Verify | 已封装 | `issue verify` | `afxx_issue(action="verify")` + `afxx_run` | 引用 deploy job 提交候选包验证；不自动 restart client。 |
| `POST /api/issues/{issue_id}/regression-cases` | Issue Pins | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `POST /api/issues/{issue_id}/knowledge` | Submit Knowledge | 已封装 | `issue submit-summary` | 仅通过 `afxx_run` | 提交已检视总结并归档，不负责生成总结。 |
| `POST /api/admin/issues/{issue_id}/close` | Close Issue | 未封装 | — | — | 平台管理权限操作，当前不纳入修复工具包。 |
| `POST /api/admin/issues/{issue_id}/merge` | Merge Issue | 未封装 | — | — | 平台管理权限操作，当前不纳入修复工具包。 |
| `GET /api/ops/summary` | Summary | 已封装 | `platform summary` | 仅通过 `afxx_run` | scope=mine/all，运行快照可见性由平台权限决定。 |
| `GET /api/ops/efficiency` | Efficiency | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `POST /api/client/heartbeat` | Heartbeat | AFX 负责 | — | — | 由 AFX client 管理；不重复实现领取、上报或心跳。 |
| `POST /api/admin/clients/update` | Client Update | 未封装 | — | — | 平台管理权限操作，当前不纳入修复工具包。 |
| `POST /api/clients/daemon` | Client Daemon | AFX 负责 | — | — | 由 AFX client 管理；不重复实现领取、上报或心跳。 |
| `DELETE /api/admin/clients/{client_id}` | Delete Client | 未封装 | — | — | 平台管理权限操作，当前不纳入修复工具包。 |
| `POST /api/client/executions` | Upload | AFX 负责 | — | — | 由 AFX client 管理；不重复实现领取、上报或心跳。 |
| `GET /api/retests` | Retest List | 已封装 | `retest list` | 仅通过 `afxx_run` | 当前用户的手动复测批次，不含 issue verify 轮次。 |
| `POST /api/retests` | Manual Retest | 未封装 | — | — | 按需补 retest submit；与 issue verify 不同。 |
| `POST /api/client/retests/claim` | Claim Retests | AFX 负责 | — | — | 由 AFX client 管理；不重复实现领取、上报或心跳。 |
| `POST /api/client/retests/{task_id}/complete` | Complete Retest | AFX 负责 | — | — | 由 AFX client 管理；不重复实现领取、上报或心跳。 |
| `GET /api/regression/preview` | Preview | 已封装 | `issue regression-preview` | 仅通过 `afxx_run` | 按 issue 预览回归候选。 |
| `GET /api/regression/pins` | Pins | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `POST /api/regression/pins` | Add Pins | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `DELETE /api/regression/pins/{pin_id}` | Revoke Pin | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `GET /api/knowledge/search` | Knowledge Search | 未封装 | — | — | 后续 knowledge search。 |
| `GET /api/knowledge/export` | Knowledge Export | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `GET /api/knowledge/{knowledge_id}` | Knowledge Item | 未封装 | — | — | 后续 knowledge show。 |
| `POST /api/knowledge` | Propose Knowledge | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `POST /api/admin/knowledge/{knowledge_id}/status` | Knowledge Status | 未封装 | — | — | 平台管理权限操作，当前不纳入修复工具包。 |
| `PUT /api/admin/settings` | Change Settings | 未封装 | — | — | 平台管理权限操作，当前不纳入修复工具包。 |
| `GET /api/config/operators` | Operators | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `GET /api/config/me` | Config Me | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `POST /api/config/me` | Put Config Me | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `GET /api/config/default` | Config Default | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `POST /api/config/default` | Put Config Default | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `POST /api/registration/apply` | Apply | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `POST /api/registration/status` | Application Status | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `POST /api/registration/claim` | Application Claim | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `GET /api/admin/users` | Users | 未封装 | — | — | 平台管理权限操作，当前不纳入修复工具包。 |
| `POST /api/admin/users/review` | Review | 未封装 | — | — | 平台管理权限操作，当前不纳入修复工具包。 |
| `POST /api/admin/users/manage` | Manage | 未封装 | — | — | 平台管理权限操作，当前不纳入修复工具包。 |
| `GET /api/overview/pipeline` | Pipeline | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `GET /api/overview/backend` | Backend | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `GET /api/overview/perf` | Perf | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `GET /api/overview/dims` | Dims | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `GET /api/overview/matrix` | Matrix | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `GET /api/profile/axes` | Axes | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `GET /api/profile/points` | Points | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `GET /api/profile/patterns` | Patterns | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `POST /api/admin/profile/drafts` | Drafts | 未封装 | — | — | 平台管理权限操作，当前不纳入修复工具包。 |
| `POST /api/import/baize` | Import Baize | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `POST /api/models/jobs` | Create Model Job | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `GET /api/models/jobs` | Model Job | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |
| `GET /api/models/jobs/{job_id}` | Model Job | 未封装 | — | — | 按需评估，不列入当前修复工作流必需项。 |

## 聚合命令与非平台工作流

这些入口不是单个 OpenAPI 的一对一包装，应与上表分开维护。

| CLI | MCP | 实现边界 |
| --- | --- | --- |
| `issue preview` | `afxx_issue(action="preview")` + `afxx_run` | 读取本地领取快照，输出失败摘要/seed；必要时调用 issue cases 补全 |
| `issue status` | `afxx_issue_status` + `afxx_run` | 根据指定 round_no 聚合 issue detail、round tasks 及回归状态 |
| `issue verify-check` | `afxx_issue(action="verify-check")` + `afxx_run` | 只读聚合验证、关联回归、任务摘要及生命周期判断；可等待，不续租 |
| `issue wait` | 仅通过 `afxx_run` | 轮询上述状态；不是新的平台 API |
| `client status/start/stop/restart` | `afxx_client` + `afxx_run` | 经 NPU 连接调用 AFX managed client，管理已有唯一 client；不是 /api/clients 的写接口 |
| `deploy` / `replay` | `afxx_deploy` / `afxx_replay` + `afxx_run` | 同步、构建、安装及 fuzz replay；replay 按 request_id 分组，每次新建 run，不依赖 deploy，不等价于平台 retest submit |
| `replay list/show/compare` | `afxx_run` | 本地流程索引与远端执行记录查询、按 seed/backend/shape 对比；不调用平台 API |
| `status` | `afxx_job_status` + `afxx_run` | 查询 AFXX 远端 job、attempt/fields 或等待变化，不查询平台 model job |
| `env init/update/check`、`doctor` | 仅通过 `afxx_run` | 环境初始化、更新和检查 |
| `evidence list` | `afxx_evidence_list` + `afxx_run` | 按 request_id/run 选择复跑，按文件游标列出远端产物与失败关联 |
| `evidence read` | `afxx_evidence_read` + `afxx_run` | 按 request_id/run 选择复跑，按字节偏移/长度读取远端证据；可传 sha256 拒绝变化内容 |
| `config init` | 仅通过 `afxx_run` | 本地 AFXX 配置，不是 /api/config/me |
| `skills install` | 无 | 本地安装随包发布的 skills |
| 无 | `afxx_operation_status` | 查询本地持久化 MCP 调用回执，不等价于远端任务完成 |

MCP 另提供 `afxx_help` 查询 CLI 帮助、`afxx_run` 通用入口。长操作先返回可查询调用回执，再用 job_id 或 round_no 跟踪实际执行；详见[Agent 调用约定](AGENT_USAGE.md)。

## 后续补齐顺序

1. 已补齐 CLI：`case list`、`issue list`、`issue cases`、`platform me`、`platform summary`；仅同步通用 MCP 入口，不新增独立工具。
2. `issue renew/detail/regression-preview/submit-summary/wait` 当前仅通过 `afxx_run`；暂不批量新增独立 MCP，按实际使用需求评估。
3. 后续：knowledge search/show；按实际需求增加手动复测提交和回归用例管理。
4. 心跳、任务领取、结果上报继续由 AFX 负责；不以全量 API 覆盖率作为目标。

## 维护约定

- 新增、删除或改名 CLI/MCP 时，同一变更更新本表、命令参考和相关测试。提议名称必须明确标为未实施，不得写入当前 CLI/MCP 列。
- 上游 OpenAPI 更新时，重新获取并记录日期、版本、路径/操作数量和文件 SHA-256，逐项核对 method + path、summary、参数和响应语义。API version 未变也可能发生接口变动。
- 独立 CLI、通用 MCP、结构化 MCP 分开标记；内部调用不标为完整封装。未实际验证的线上行为不得写成已验证。
- 表格是实现索引，不复制完整 OpenAPI schema；参数约束以实现与上游协议共同核对，避免维护第二份易漂移的协议。
