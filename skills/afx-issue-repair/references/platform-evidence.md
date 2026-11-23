# 平台证据与阻塞处理

在需要失败任务、回归候选或执行日志时读取本文件。统一调用 AFXX CLI/MCP，由 AFXX 处理鉴权、响应和错误；skill 不自行拼 HTTP 请求，不直接读取 token。

## 获取证据

命令默认使用固定用户配置；仅使用非默认配置时追加 `--config <配置绝对路径>`，MCP 对应传 config 字段。这些只读操作无需先领取问题，也不连接 NPU。

| 目的 | AFXX 命令 | 使用方式 |
|---|---|---|
| 生命周期与轮次 | issue detail --issue-id <issue-id> | 检查 status、rounds、events 和租约 |
| 失败任务 | issue tasks --issue-id <issue-id> --round-no <round-no> | 读取 tasks 中的 case_key、芯片、result、detail 和 attempts；回归失败使用 regression.round_no |
| 回归候选集 | issue regression-preview --issue-id <issue-id> | 预览不等于最终实际执行集 |
| 执行详情与日志 | execution show --exec-uuid <exec_uuid> | 使用已有可核验执行 UUID，读取环境、Shape、日志及缺失/截断标记 |

`execution show` 的参数是 exec_uuid，不是数值 exec_id。任务接口不保证直接给出 exec_uuid；缺少它时先检查已有领取信息、任务 detail 和远端 client/replay 日志，或让用户提供平台执行链接/ID，不拼凑 ID，也不绕过 AFXX 发起 HTTP 查询。log_truncated=true 表示日志被平台截断，log_missing=true 表示日志内容缺失，不能把它们作为完整证据。

已有 claim 返回的 related_knowledge 可直接阅读；当前不扩展 knowledge show/search。修复总结先在本地整理并等待人工检视，不自动发布平台知识或关闭 issue。

## 分清三类失败

- 代码失败：具有候选包身份匹配的编译、正确性或性能失败证据。回定位/修改，再新部署、新验证。
- 环境或执行失败：依赖缺失、NPU 占用、网络不可达、结果 invalid、包不匹配。先处理环境，不据此改变算法或宣称修复失败。
- 不确定状态：超时、响应丢失、任务仍 pending。查询原 job/round；提交 verify 的响应丢失时重试同一 deploy job，复用幂等请求。

## 常驻客户端边界

AFXX 的 verify 使用配置 venv/CANN 调用 AFX managed 接口，client 目录采用远端 AFX 的当前目录，优先 AUTOFUSEX_HOME，再读取默认目录的 active-home.json，最后使用系统默认目录。目标是每个 NPU 环境只有一个 client，所有修复任务复用同一目录和身份；daemon、执行会话和 worker 是这个 client 的组成部分，不是额外 client。

- PLATFORM_CLIENT_START_FAILED：查看 client.log，核对 endpoint、client token 权限、NPU 可见性和官方命令版本；未提交 verify。
- PLATFORM_CLIENT_MISMATCH：managed 配置中的 endpoint、token 或设备与当前配置不同。AF 包哈希变化允许复用已有进程，领取新包任务依赖 AFX 刷新包身份。deploy 不会自动停止/刷新这个进程；需要时使用显式 client stop/restart 接口协调。不要手改 launch.json、删除记录或跳过校验来强行提交。
- PLATFORM_CLIENT_UNTRACKED：迁移目录中仍有旧版 client 存活，不能再启动一个 managed client 与其并行领取。

没有在本次任务中授权客户端协调时，报告进程/日志、候选身份及所需处理，等待用户处理或明确授权；不自行 kill 常驻进程。已有明确授权时在其范围内处理，不重复请求。AFX 环境信息可能缓存，不能只看到 PID 存活就认为它识别了新包。


平台复测派发问题待平台侧定位。已观察到 runner 环境中 AF 哈希缺失、installed 为空，但这不等同于已确认平台派发问题的完整根因；保留轮次、pending/领取记录、执行身份与日志作为证据。仅看到心跳在线或 fuzz 运行不能宣称复测已接收。本阶段不改领取协议、不引入复测专用的第二个 client，也不将“只待命复测”写成现有能力。


## 检视后的独立总结提交

仅在总结经过人工检视且用户明确要求提交时，使用 `afxx issue summarize --issue-id <issue-id> --summary-file <本地summary.yaml> --request-id <本次总结请求ID>`；不是修复循环的自动步骤。直接使用修复流程生成并检视过的 summary.yaml，无需再转换格式。YAML 是 AFXX 的本地输入约定，平台实际接收 JSON。YAML 直接包含 kind、title、symptom、root_cause、solution，可用 fix_ref 记录真实修复 commit，用 body_md 保存多行正文。 使用 [总结模板](../../afx-issue-summary/assets/summary.yaml) 保留本次 verify 和关联回归的结论与精确轮次引用；平台自动关联 issue 不替代对当前修复证据的核对。正文摘录最小实际错误片段，展开根因与修复分析、人工检视重点及同类定位方法；不以 job 引用代替技术解释，不复制整份报告或部署流水账。

需要本地领取记录、有效租约、knowledge 权限及平台 summarizing 状态。网络重试复用原文件和 request-id；内容或租约改变不能复用旧键。只有返回 status=resolved 和 knowledge_id 才报告平台归档完成。此命令会发布知识，而非保存待审草稿。

生命周期统一调用 `afxx client status/start/stop/restart`（带同一任务配置），不自行拼 SSH。status 只查询，stop 排空并退出 daemon，restart 在停止完成后启动；PLATFORM_CLIENT_TIMEOUT 不代表已停止，PLATFORM_CLIENT_CONFLICT 表示存在其他 client，PLATFORM_CLIENT_MIGRATION_REQUIRED 表示旧身份迁移尚未明确。start/stop/restart 返回成功只证明生命周期操作完成，retest_readiness=not_checked；平台复测派发仍待定位。
