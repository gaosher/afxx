---
name: afx-issue-repair
description: 使用 AFXX 领取 AFX 平台 issue，复现并定位 graph-autofusion 问题，实施修复、部署、提交验证与等待回归，通过后整理总结并 commit 等待检视。用于用户要求执行这套修复闭环或继续已有修复任务时。
---

# AFX Issue 修复

目标：claim → 复现与定位 → 修改 → deploy → verify → 回归 → 总结、commit、待检视。失败时依据证据返回定位；环境或权限阻塞单独报告。创建/讨论 workflow 本身不代表要立即领取问题。

## 执行约定

- AFXX 在本地调用，NPU 作为执行资源。使用已配置的 `afxx`，或对应的结构化 MCP 工具；只有缺少对应工具时才用 `afxx_run`，不熟悉参数先查 `afxx_help`；不要重写同步、编包、安装或客户端复测循环。
- 开始时查 `afxx help` 及本次需要的子命令帮助，以已安装版本为准。环境命令可能分组为 `env init/update/check`；所有命令默认读取 `~/.config/afxx/config.yaml`，用户指定配置时使用该路径；不能因复现需要就擅自替换既有环境。
- 启动时将选定配置展开为绝对路径，展示配置路径、connection.environment、workflow_root、local.source_dir 和平台地址（不输出 token）。默认配置不存在时提示 `afxx config init --source <源码目录>` 或请求已有配置路径，不搜索环境变量或当前目录。领取阶段示例使用默认配置；用户指定其他配置时追加 `--config <绝对路径>`。创建 worktree 任务配置后，按第 2 步切换，后续示例显式携带该配置（MCP 传 `config`）。
- 从配置定位 `local.source_dir`，在目标源码仓库读取 AGENTS.md，遵循其开发、测试、构建和提交要求。先记录原有修改，不覆盖或提交用户无关改动。
- 用户要求执行完整修复流程时，按授权范围领取、部署、verify 和本地 commit；已有授权不重复询问。只做诊断或讨论时不发起这些写操作。不自动 push、合并或关闭 issue。
- 开始修复时说明本次配置的平台地址和数据范围：复跑默认向该平台上传执行结果及相关诊断；dump 在远端采集、按需读取，不自动上传整个 dump 目录。
- 用户已明确授权同一平台及数据范围后，后续修复轮次沿用授权，不重复询问；目标平台或数据范围扩大时重新核对。重试前先核对是否已提交，避免重复复跑。
- 远端 verify 使用 `platform.remote_token_file`，AFX client 使用 `platform.client_token_file`。配置 platform 后，env init 会复用或隐藏输入本地 token，并在环境成功后同步；异步 init 用 status/wait 完成同步。修复时复用已有凭据，遇到缺失或鉴权错误报告具体字段；不要为领取问题重建环境或重新签发 token。
- 每个目标 NPU 环境只维护一个 AFX client，跨 issue、worktree 和部署轮次复用同一 client home 与身份。不要为单个问题初始化新 client、生成新 source_id 或另起旧版客户端；发现重复身份时先核对已有进程和平台记录，不自动删除平台记录。
- 一个问题的工作记录保留 issue_id、租约到期时间、基线/候选 deploy run、request-id、verify round_no、AF 包身份及证据路径。复用任务已有笔记或修复总结，不另造状态数据库、不复制 wheel/源码快照。
- 使用 MCP 时优先调用结构化工具。deploy、client start/stop/restart 和 issue verify 等长操作先选定工具级 request_id，提交后用 afxx_operation_status 查询返回的回执 ID（deploy/verify 自动分配，区别于修复分组）；completed 只表示 CLI 调用结束，必须检查 data.result.ok，再从其中取得 request_id/run 或 round_no 或 client running 状态。MCP 断开后继续查询原 request_id；unknown 时先核对远端，不换键盲目重试。restart 的调用记录成功后才进入 verify，不因收到 pending 回执就继续。
- 部署断线后先查询 deploy list/show，不盲目重复提交；源码/配置改变后继续使用同一修复 request-id，每次 deploy 自动新增 run。同组、同租约、同包的 verify 重试取回原回执，包变化后生成新验证。

## 1. 领取 issue

```bash
afxx doctor
afxx client status
afxx issue claim --issue-id <issue-id> --request-id <本次领取请求ID>
# 指定问题时增加 --issue-id F152
```

保存实际返回的 issue_id，不固定使用示例编号。data=null 表示暂无任务，正常结束并报告，不循环刷 claim。继续已有任务时先读取其领取记录和租约，不重复领取。

```bash
afxx issue preview --issue-id <issue-id> --full
```

读取复现信息、失败摘要和 related_knowledge。preview 是受影响用例，不等于平台回归候选集。不另安排定时续租；恢复搁置任务或遇到租约错误时核对所有权和到期时间，必要时执行 `issue renew`。租约失效或被接管后停止写操作。

## 2. 复现并收集定位信息

原始基线优先使用 graph-autofusion 的 `upstream/master`。确认 upstream 指向目标上游仓库，获取最新 master 并记录解析后的确切 commit SHA；本轮复现和修复固定使用这个 SHA，不随上游移动而改变基线。

在该 SHA 上建立干净的独立 worktree，任务配置的 local.source_dir 指向它；首次复现前执行下面的“deploy 主线基线 → 等待成功 → replay 复现”。修复分支从同一基线继续；已有基线部署只有满足下述全部条件才可复用。保留原工作区及配置，不通过 reset、清理或切分支覆盖用户已有修改。若用户指定其他修复分支，单独记录其与基线的差异，不把它冒充上游原始基线。upstream/master 不存在或获取失败时明确报告，不能静默替换为 origin/master、缓存引用或当前工作区。

使用 worktree 时，复制配置并仅将 `local.source_dir` 改为 worktree 绝对路径，不覆盖默认配置。从此步开始，所有后续操作（包括 status、issue 查询和 verify）必须带 `--config "$TASK_CONFIG"`，MCP 显式传同一路径；仅 cd 到 worktree 不会切换配置。deploy 前核对源码路径，无需 env update。

```bash
TASK_CONFIG="/absolute/path/to/task-config.yaml"
```

以下示例按 worktree 任务配置编写；未创建任务配置且使用默认配置时，省略 `--config "$TASK_CONFIG"`。

基线和候选复现均使用 `replay --dump`，通过 `afx fuzz replay` 复用现有队列。复跑结果统一上传。读取 [复现证据清单](references/replay-evidence.md)，仅把明确路径关联的 dump 当作本轮证据；不自动重启 client 开启全局 dump。

### 2.1 Deploy 主线基线

修改源码前，先部署上述主线 worktree。deploy 会同步未提交修改，提交前必须确认 worktree 干净，不包含会被同步的额外源码文件。基线与每轮候选 deploy 前均执行[部署期间的客户端协调](references/platform-evidence.md#部署期间的客户端协调)，确认允许协调目标客户端；由 deploy 在包准备好之后停止并恢复，不能只在候选部署时处理。

```bash
afxx deploy --config "$TASK_CONFIG" --coordinate-client --request-id <修复流程ID>
```

记录返回的 `request_id/run` 为基线部署记录，并保存对应的主线 SHA。这个步骤仅部署，不提交平台 verify。

仅当以下条件全部满足时，才可跳过本次 deploy，复用已有基线 job：

- 该 job 已成功，源码清单对应本次固定的 upstream/master SHA，且没有额外源码差异；仅有相同 HEAD 或包版本号不足以证明一致。
- deploy 的来源、源码清单和安装身份仍可读取核验；远端当前加载的 AF 路径及 pyautofuse.so 哈希与该 job 的 result.json 一致，没有被后续安装替换。
- 该部署属于本次目标 NPU、CANN 安装目录和运行环境；由 agent 核对来源，replay 本身不检查 deploy 引用。

记录复用的 job ID、SHA 和核验依据；任一条件不满足或无法确认，都重新部署主线基线。不能用来源不明的远端现有包代替。

### 2.2 等待基线部署成功

```bash
afxx deploy show --config "$TASK_CONFIG" --request-id <修复流程ID> --run <基线run>
```

只有 `state=success` 才进入复现，并按部署前记录恢复需要的客户端会话、核对实际 worker 使用的 AF 身份。queued/running 或提交响应的 ok=true 不代表部署完成；failed/unknown 时按客户端协调说明处理恢复，不继续 replay，也不能宣称已复现。

### 2.3 Replay 复现原问题

对 nnsmith seed，使用本次修复的流程 request_id。replay 复用现有队列；先确认需要的基线已安装，不能以提交进程的环境替代 worker 身份：

```bash
afxx replay --config "$TASK_CONFIG" --dump --seed <原始seed> --request-id <流程request-id>
afxx replay show --config "$TASK_CONFIG" --request-id <流程request-id> --run <run>
```

记录 replay 返回的 request_id/run，所有修复轮次复用同一流程 request_id；每次调用都会执行，不需要 --new-run。用 replay list 查看历史、replay show --run N 查看指定一次、replay compare --runs N M 对比。MCP 的 afxx_replay 直接返回 request_id/run，通过 afxx_run 调用上述查询，不查 operation_status。断线先查历史，不能盲目重复 replay。

若 replay 在准备阶段退出，先按[准备阶段失败](references/replay-evidence.md)定位出错的子命令。目标 seed 实际执行且失败阶段和关键诊断与平台记录吻合后，才能报告已复现。

用 replay show 查询指定 run 直到终态并取得 evidence_path，再按 [复现证据清单](references/replay-evidence.md) 读取本地报告及日志；dump 使用下面的 evidence 命令按需读取远端文件。缺失证据注明原因；保持原 seed、输入和精度语义。

```bash
afxx evidence list --config "$TASK_CONFIG" --request-id <流程request-id> --run <run>
afxx evidence read --config "$TASK_CONFIG" --request-id <流程request-id> --run <run> \
  --path <list返回的远端文件路径> --offset 0 --limit 8192
```

省略 --run 时查询最新一次，但翻页、续读和对比时必须固定首次返回的 run；不要在出现新复跑后混读另一轮。read 续读传 next_offset 和上次 sha256。MCP 使用 afxx_evidence_list/read(request_id=..., run=...)，定位 replay 设置 dump=true。复跑结果统一上传。

非 nnsmith 用例使用平台给出的复现方式和目标环境，不强行传入 AFXX seed 接口。失败无法复现时，先比较环境、包版本和用例条件；明确“未复现”及缺失证据，不直接宣称修复。

## 3. 实施修改

以复现证据形成根因假设，定位到源码后实施最小完整修复。依据目标仓库规范增加或调整能表达该缺陷的测试，先执行相关验证。区分算法/编译问题和环境、资源、任务缺失问题。

每轮记录“新证据 → 修改原因 → 预期改变”。不要仅为某个 seed 硬编码，不修改平台验收标准。连续重复同一失败而没有新证据时，停止盲目部署，先补定位信息；需要外部权限、资源或行为决策时报告阻塞点。

## 4. 部署修复版本

每轮部署使用 `--coordinate-client`，执行前阅读[部署期间的客户端协调](references/platform-evidence.md#部署期间的客户端协调)。由远端 worker 在编包完成后停止客户端、安装并恢复；agent 不再拆开执行 stop/deploy/start。

```bash
afxx deploy --config "$TASK_CONFIG" --coordinate-client --request-id <修复流程ID>
afxx deploy show --config "$TASK_CONFIG" --request-id <修复流程ID> --run <候选run>
```

部署等待时查询同一 request_id/run 的 deploy show；等待 state=success，保留源码和实际包身份。断线先查记录，不重复部署。构建或安装失败不进入 verify。replay 使用 replay show 查询。

环境一致性 WARNING 不会自动阻止 AFXX；agent 应读懂差异，并确认候选环境能用于该问题。不能把带差异的配置视为精确复现证据。平台复测执行期间不覆盖它使用的候选环境。

## 5. 提交并等待 verify

每轮候选 deploy 成功后，首次提交该候选的 verify 前，先查询 client status。若 deploy 返回 `client_coordination.state=restored`、原状态为 running，且当前仍运行，则复用本轮已核验的恢复结果，不重复重启。恢复为 idle 或原本 stopped 时，不把它当作测试已启动；按本次测试授权启动。否则，存在唯一存活 client 时重启其实际 home；没有存活 client 时使用 client start，多个存活 client 报错时停止流程，不自行挑选或强杀：

```bash
afxx client restart --config "$TASK_CONFIG" --timeout 300
```

复用已核验且仍 running 的部署恢复结果，或本次 restart/start 返回 `ok=true` 且 `data.state=running` 后，才提交下面的 verify。失败或超时时先用 `client status` 检查并处理，不继续提交，不手工 kill 或新建 client。此重启是本修复流程的必要步骤，包含等待在途任务结束；已有完整修复流程授权时无需重复确认。重启可能按平台配置恢复 fuzz，但不代表复测领取或平台派发已经正常。

同一候选的 verify 已提交或响应丢失时，先查询已有轮次或按原请求幂等重试，不为等待、轮询或提交重试再次重启，避免打断在途复测。

```bash
afxx issue verify --config "$TASK_CONFIG" --issue-id <issue-id> --request-id <修复流程ID>
```

verify 在远端采集当前安装的 AF 身份，不选择历史部署，确保环境中唯一的 AFX client 启动，再向平台提交；已有 client 沿用 AFX 保存的凭据，首次创建时才使用 `platform.client_token_file`。保存返回的 verify round_no，`submitted` 只表示提交成功，client/session running 也不证明平台已派发或本机已领取。平台复测派发问题当前待平台侧定位；排队、领取和执行结果分别取证，不通过启动额外 client、重复提交 verify 或修改 AFX 领取逻辑绕过。

```bash
afxx issue verify-check --config "$TASK_CONFIG" --issue-id <issue-id> --round-no <verify-round-no> --wait --timeout 30
```

agent 使用 issue verify-check 等待原验证轮次；MCP 调用 afxx_issue(action="verify-check", issue_id=..., round_no=..., wait=true, timeout=30)。这是只读检查，不续租、不重启、不重复提交；timed_out=true 时继续查询同一轮次。以 state=passed 为通过，failed/invalid/attention 均需检查，running 继续等待。只有 reason=no_regression_candidates 才记录平台未选择回归。只在阶段变化、新失败或完成时汇报。

- verification 未结束：继续等待。
- verification.outcome=failed：读取该轮失败任务与执行证据，回到第 2/3 步；修改后经过新的 deploy 和 verify。
- state=invalid：优先排查包身份、结果缺失和执行环境，不等同于代码缺陷。
- verification.outcome=passed：转第 6 步；不能提前 commit 为已验证修复。

失败任务查询、环境阻塞和客户端身份处理按 [平台定位参考](references/platform-evidence.md)。尤其第二轮 deploy 后可能遇到存活客户端的旧 AF 身份：先调用 `client status`，在已有客户端协调授权范围内使用 `client stop/restart`，不绕过检查或误报循环成功。未传 `--coordinate-client` 的 deploy 不协调 client；传入后协调失败必须先处理，不绕过恢复检查。

## 6. 等待关联回归

平台在 verify 通过后自动选择并执行回归，无需再次提交 verify 来“触发回归”。继续查询同一个 verify round_no；AFXX 返回其 verification 和关联 regression。

- regression 存在且未结束：继续等待。
- regression.outcome=failed：提取新增失败用例和证据，回到第 2/3 步；保留原问题 seed 和本次回归失败用例作为后续验证目标。
- regression.outcome=invalid：处理执行环境和证据问题后再决定重试，不能计为通过。
- state=passed 且 regression.outcome=passed：转第 7 步。
- regression=null：继续使用 verify-check；仅 state=passed 且 reason=no_regression_candidates 时记录“平台未选择回归候选”，而非“回归用例全部通过”。running 继续等待，attention 检查证据；若用户要求必须执行回归，则报告缺口并补足要求后再完成。

只使用本次候选的轮次证据，不拿后续其他修复的通过结果替代当前结果。

## 7. commit、总结交接、等待检视

1. 核对将提交的源码与已验证部署一致；影响产物的代码若再次变化，重新部署验证。
2. 遵循目标仓库规范，只提交本次修复和必要测试/文档，记录完整 SHA。用户要求仅出补丁或不提交时遵循该要求。
3. 读取并使用 [afx-issue-summary](../afx-issue-summary/SKILL.md) 生成可提交的 summary.yaml。交接 issue、源码仓库、基线与修复 commit、已有 IR/日志/测试证据位置、平台 verify/回归轮次、任务配置及输出路径 `<local.state_dir>/issues/<issue-id>/summary.yaml`。直接沿用已有任务上下文，不另建交接文件；本地路径用于取证，不进入总结正文。
4. 向用户交付修复 commit、summary.yaml 绝对路径和验证结论，等待人工检视。总结规则与模板由 afx-issue-summary 维护，不在这里重复。

不自动发布总结。人工检视且用户明确要求提交后，再按 [平台提交说明](references/platform-evidence.md) 执行独立的 issue submit-summary；若检视导致代码变化，重新验证受影响内容并更新总结。不得将测试通过推断为已经合入或平台归档完成。
