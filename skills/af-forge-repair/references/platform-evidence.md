# 平台证据与阻塞处理

在需要失败任务、回归候选或执行日志时读取本文件。统一调用 Forge CLI/MCP，由 Forge 处理鉴权、响应和错误；skill 不自行拼 HTTP 请求，不直接读取 token。

## 获取证据

命令默认使用固定用户配置；仅使用非默认配置时追加 `--config <配置绝对路径>`，MCP 对应传 config 字段。这些只读操作无需先领取问题，也不连接 NPU。

| 目的 | Forge 命令 | 使用方式 |
|---|---|---|
| 生命周期与轮次 | issue detail --issue-id <issue-id> | 检查 status、rounds、events 和租约 |
| 失败任务 | issue tasks --issue-id <issue-id> --round-no <round-no> | 读取 tasks 中的 case_key、芯片、result、detail 和 attempts；回归失败使用 regression.round_no |
| 回归候选集 | issue regression-preview --issue-id <issue-id> | 预览不等于最终实际执行集 |
| 执行详情与日志 | execution show --exec-uuid <exec_uuid> | 使用已有可核验执行 UUID，读取环境、Shape、日志及缺失/截断标记 |

`execution show` 的参数是 exec_uuid，不是数值 exec_id。任务接口不保证直接给出 exec_uuid；缺少它时先检查已有领取信息、任务 detail 和远端 client/replay 日志，或让用户提供平台执行链接/ID，不拼凑 ID，也不绕过 Forge 发起 HTTP 查询。log_truncated=true 表示日志被平台截断，log_missing=true 表示日志内容缺失，不能把它们作为完整证据。

已有 claim 返回的 related_knowledge 可直接阅读；当前不扩展 knowledge show/search。修复总结先在本地整理并等待人工检视，不自动发布平台知识或关闭 issue。

## 分清三类失败

- 代码失败：具有候选包身份匹配的编译、正确性或性能失败证据。回定位/修改，再新部署、新验证。
- 环境或执行失败：依赖缺失、NPU 占用、网络不可达、结果 invalid、包不匹配。先处理环境，不据此改变算法或宣称修复失败。
- 不确定状态：超时、响应丢失、任务仍 pending。查询原 job/round；提交 verify 的响应丢失时重试同一 deploy job，复用幂等请求。

## 常驻客户端边界

Forge 的 verify 使用配置 venv/CANN 调用 AFX 官方 CLI，启动记录与日志位于 remote.state_dir 上一级目录的 afx-client/。

- PLATFORM_CLIENT_START_FAILED：查看 client.log，核对 endpoint、client token 权限、NPU 可见性和官方命令版本；未提交 verify。
- PLATFORM_CLIENT_MISMATCH：存活客户端的启动命令或环境变量与当前配置不同。AF 包哈希变化允许复用已有进程，领取新包任务依赖 AFX 刷新包身份。当前 Forge 不会停止/刷新这个进程；第二轮修复可能因此阻塞。不要手改 launch.json、删除记录或跳过校验来强行提交。
- PLATFORM_CLIENT_UNTRACKED：同一 HOME 的现有进程缺少匹配启动证据，不能假定属于本任务。

没有在本次任务中授权客户端协调时，报告进程/日志、候选身份及所需处理，等待用户处理或明确授权；不自行 kill 常驻进程。已有明确授权时在其范围内处理，不重复请求。AFX 环境信息可能缓存，不能只看到 PID 存活就认为它识别了新包。


## 检视后的独立总结提交

仅在总结经过人工检视且用户明确要求提交时，使用 `af-forge issue summarize --issue-id <issue-id> --summary-file <本地summary.yaml> --request-id <本次总结请求ID>`；不是修复循环的自动步骤。直接使用修复流程生成并检视过的 summary.yaml，无需再转换格式。YAML 是 Forge 的本地输入约定，平台实际接收 JSON。YAML 直接包含 kind、title、symptom、root_cause、solution，可用 fix_ref 记录真实修复 commit，用 body_md 保存多行正文。

需要本地领取记录、有效租约、knowledge 权限及平台 summarizing 状态。网络重试复用原文件和 request-id；内容或租约改变不能复用旧键。只有返回 status=resolved 和 knowledge_id 才报告平台归档完成。此命令会发布知识，而非保存待审草稿。
