---
name: af-forge-repair
description: 使用 AF Forge 领取 AFX 平台 issue，复现并定位 graph-autofusion 问题，实施修复、部署、提交验证与等待回归，通过后整理总结并 commit 等待检视。用于用户要求执行这套修复闭环或继续已有修复任务时。
---

# AF Forge 问题修复

目标：claim → 复现与定位 → 修改 → deploy → verify → 回归 → 总结、commit、待检视。失败时依据证据返回定位；环境或权限阻塞单独报告。创建/讨论 workflow 本身不代表要立即领取问题。

## 执行约定

- Forge 在本地调用，NPU 作为执行资源。使用已配置的 `af-forge`，或 MCP 的 `af_forge_help` / `af_forge_run`；不要重写同步、编包、安装或客户端复测循环。
- 开始时查 `af-forge help` 及本次需要的子命令帮助，以已安装版本为准。环境命令可能分组为 `env init/update/check`；初始化和更新必须显式指定配置，不能因复现需要就擅自替换既有环境。
- 确定本次配置的绝对路径，后续 CLI 命令显式传 `--config`，MCP 调用传 `config`，防止 cwd 改变后连接其他环境。下文示例中 `$CONFIG` 是选定的本地配置文件。
- 从配置定位 `local.source_dir`，在目标源码仓库读取 AGENTS.md，遵循其开发、测试、构建和提交要求。先记录原有修改，不覆盖或提交用户无关改动。
- 用户要求执行完整修复流程时，按授权范围领取、部署、verify 和本地 commit；已有授权不重复询问。只做诊断或讨论时不发起这些写操作。不自动 push、合并或关闭 issue。
- 一个问题的工作记录保留 issue_id、租约到期时间、基线/候选 deploy job_id、request-id、verify round_no、AF 包身份及证据路径。复用任务已有笔记或修复总结，不另造状态数据库、不复制 wheel/源码快照。
- 网络重试复用同一请求与 request-id；源码/配置改变后使用新的 deploy request-id。一个候选 job 的 verify 重试取回原回执，不能用它开始新的修复轮次。

## 1. 领取 issue

```bash
af-forge doctor --config "$CONFIG"
af-forge issue claim --config "$CONFIG" --request-id <本次领取请求ID>
# 指定问题时增加 --issue-id F152
```

保存实际返回的 issue_id，不固定使用示例编号。data=null 表示暂无任务，正常结束并报告，不循环刷 claim。继续已有任务时先读取其领取记录和租约，不重复领取。

```bash
af-forge issue preview --config "$CONFIG" --issue-id <issue-id> --full
```

读取复现信息、失败摘要和 related_knowledge。preview 是受影响用例，不等于平台回归候选集。当前目标平台的问题租约已延长为 5 天，日常修复不安排定时续租；以 claim 返回的 lease_expires_at 为准，不将 5 天写死为所有平台的保证。仅在恢复长期搁置的任务、临近到期或收到租约错误时检查，仍由当前持有者继续处理且租约有效时按需调用 `issue renew`。租约已失效或被接管时先确认所有权，不继续该问题的写操作。Forge 内部已有的自动续租不需要 skill 重复编排。

## 2. 复现并收集定位信息

原始基线优先使用 graph-autofusion 的 `upstream/master`。确认 upstream 指向目标上游仓库，获取最新 master 并记录解析后的确切 commit SHA；本轮复现和修复固定使用这个 SHA，不随上游移动而改变基线。

优先复用来源恰好为该 SHA、无额外源码差异且仍可核验的成功 deploy job。否则在该 SHA 上建立干净的独立 worktree，使用任务专用配置将 local.source_dir 指向它，再部署基线复现；修复分支也从该基线继续。保留原工作区及配置，不通过 reset、清理或切分支覆盖用户已有修改。若用户指定其他修复分支，单独记录其与基线的差异，不把它冒充上游原始基线。upstream/master 不存在或获取失败时明确报告，不能静默替换为 origin/master、缓存引用或当前工作区。

**worktree 与 deploy 配置必须一致。** Forge 只同步配置中的 `local.source_dir`，不会随命令执行目录自动切换。进入修复 worktree 后，复制本次配置为任务专用配置，将 `local.source_dir` 改成该 worktree 的绝对路径，并将 `$CONFIG` 指向这份配置。保留 connection、远端目录和 local.state_dir，确保继续使用同一目标环境和领取记录。不要覆盖原始默认配置。

```yaml
local:
  source_dir: "/absolute/path/to/repair-worktree"
```

每次 deploy 前核对 `$CONFIG` 的 local.source_dir 与实际修改所在 worktree 一致，使用 `af-forge deploy --config "$CONFIG" ...`；MCP 也必须传入同一任务配置。未提交源码修改会被同步，不能误用仍指向原仓库的配置。

复现阶段默认在任务专用 `$CONFIG` 中设置 `replay.dump: true`，保留其他 replay 配置；不修改用户的原始配置，无需 env update。基线与候选的定位复测使用相同采集设置。执行前读取 [复现证据清单](references/replay-evidence.md)，按失败阶段收集证据。

```yaml
replay:
  dump: true
```

对 nnsmith seed 使用 Forge replay：

```bash
af-forge replay --config "$CONFIG" --job-id <基线deploy-job-id> --seed <原始seed> --request-id <复现请求ID>
af-forge status --config "$CONFIG" --job-id <replay-job-id>
```

异步提交成功不等于复现完成；用 status/wait 等到 success/failed，Forge 自动下载全量现场到 /tmp，返回 evidence_path 和失败基础信息。先读取本地 evidence.json 的 files/skipped，再按需读取 job/report.json 的 runtime、attempts 和诊断字段及失败对应日志/IR；不要将整份报告或全部 dump 放入上下文。使用清单中的 local_path，按报告中的明确关联连接 attempt 与产物，不按文件名猜测 Shape 归属。需要额外执行信息时用 `status --attempt <id> --fields ...`。下载失败时重查同一 job，不重新 replay；缓存完整则复用。按复现证据清单记录输入、环境、包身份、失败阶段和实际文件路径；缺失证据注明原因。保持原输入和精度语义，不能换 seed 或放宽正确性判定制造通过。

非 nnsmith 用例使用平台给出的复现方式和目标环境，不强行传入 Forge seed 接口。失败无法复现时，先比较环境、包版本和用例条件；明确“未复现”及缺失证据，不直接宣称修复。

## 3. 实施修改

以复现证据形成根因假设，定位到源码后实施最小完整修复。依据目标仓库规范增加或调整能表达该缺陷的测试，先执行相关验证。区分算法/编译问题和环境、资源、任务缺失问题。

每轮记录“新证据 → 修改原因 → 预期改变”。不要仅为某个 seed 硬编码，不修改平台验收标准。连续重复同一失败而没有新证据时，停止盲目部署，先补定位信息；需要外部权限、资源或行为决策时报告阻塞点。

## 4. 部署修复版本

```bash
af-forge deploy --config "$CONFIG" --issue-id <issue-id> --request-id <本轮唯一部署请求ID>
af-forge status --config "$CONFIG" --job-id <deploy-job-id>
```

部署和 replay 等待时，保存首次 status 的 cursor，使用 `status --wait --since <cursor> --timeout 30`（带同一配置和 job ID）；无变化只得到 timeout 回执，继续沿用返回 cursor，不重复读取概要或提交任务。无 cursor 时该命令以首次查询为基线。replay 终态直接返回 state、evidence_path、failures，不再需要 cursor。event=attention 表示状态不确定，应排查进程。等待 job state=success，并保留部署记录和源码/包身份。构建或安装失败先处理对应失败，不进入 verify。候选部署后可用同一原始 seed 快速 replay；平台验证仍是下一步独立验收。

环境一致性 WARNING 不会自动阻止 Forge；agent 应读懂差异，并确认候选环境能用于该问题。不能把带差异的配置视为精确复现证据。平台复测执行期间不覆盖它使用的候选环境。

## 5. 提交并等待 verify

```bash
af-forge issue verify --config "$CONFIG" --issue-id <issue-id> --job-id <deploy-job-id>
```

verify 在远端核对候选 AF 身份，确保官方 AFX client 启动，再向平台提交；客户端 token 由配置的 `platform.client_token_file` 提供。保存返回的 verify round_no，`submitted` 只表示提交成功。

```bash
af-forge issue status --config "$CONFIG" --issue-id <issue-id> --round-no <verify-round-no>
```

agent 使用 `issue wait --timeout 30` 等待原轮次，不另安排例行续租；等待内部仅在状态变化时报告，超时仅保留问题/轮次引用。超时继续等待原轮次，不重复提交。只在新阶段、新失败或完成时向用户汇报。

- verification 未结束：继续等待。
- verification.outcome=failed：读取该轮失败任务与执行证据，回到第 2/3 步；修改后经过新的 deploy 和 verify。
- outcome=invalid：优先排查包身份、结果缺失和执行环境，不等同于代码缺陷。
- verification.outcome=passed：转第 6 步；不能提前 commit 为已验证修复。

失败任务查询、环境阻塞和客户端身份处理按 [平台定位参考](references/platform-evidence.md)。尤其第二轮 deploy 后可能遇到存活客户端的旧 AF 身份：当前 Forge 不自动 stop/restart，此时报告阻塞，不绕过检查或误报循环成功。

## 6. 等待关联回归

平台在 verify 通过后自动选择并执行回归，无需再次提交 verify 来“触发回归”。继续查询同一个 verify round_no；Forge 返回其 verification 和关联 regression。

- regression 存在且未结束：继续等待。
- regression.outcome=failed：提取新增失败用例和证据，回到第 2/3 步；保留原问题 seed 和本次回归失败用例作为后续验证目标。
- regression.outcome=invalid：处理执行环境和证据问题后再决定重试，不能计为通过。
- regression.outcome=passed 且 terminal=true、outcome=passed：转第 7 步。
- regression=null：用 `issue detail --issue-id <issue-id>` 查询平台详情；确认平台已进入 summarizing/resolved，且本次 verify 确实通过、没有关联回归时，记录“平台未选择回归候选”，而非“回归用例全部通过”。若用户要求必须执行回归，则报告缺口并补足该要求后再完成。

只使用本次候选的轮次证据，不拿后续其他修复的通过结果替代当前结果。

## 7. 总结、commit、等待检视

完成本轮相关测试、verify 和关联回归后，必须生成可直接提交的 `summary.yaml`，不另生成一份需要手工转换格式的总结。使用 [总结模板](assets/summary.yaml)，直接填写平台知识字段，不加 knowledge 外层，不自建 evidence、issue_id 等顶层字段；修复和验证证据写入 `body_md`。

1. 先核对当前将提交的源码与已验证部署一致。若验证期间又修改了影响产物的代码，重新部署验证，不能用旧包结果证明新代码。
2. 遵循目标仓库规范，只暂存本次修复及必要测试/文档，创建本地 commit 并记录完整 SHA。用户要求仅出补丁或不提交时遵循该要求，不编造 fix_ref。
3. 将总结保存到本机 `<local.state_dir>/issues/<issue-id>/summary.yaml`，放在源码 worktree 外，不随源码同步或纳入本次修复 commit。这样可以在 commit 完成后填入真实 `fix_ref`，避免总结记录自身 commit SHA 的循环。
4. 填写 kind、title、symptom、root_cause、solution；有修复 commit 时必须填 fix_ref。body_md 包含 issue、基线 SHA、触发条件/seed/Shape、修改范围、测试命令和结果、基线与候选 deploy job_id、AF 版本及 pyautofuse.so SHA-256、verify/regress 轮次与结果、日志/报告位置、限制和检视重点。没有回归候选时明确记录平台未选择候选，不能写成回归用例全部通过。
5. 生成后检查 YAML 可解析、必填项非空、证据与实际记录一致、模板占位符已全部替换或删除。尚未获取的信息明确写“未获取”并说明原因，不伪造 SHA、用例数或通过结果。不要包含 token、凭据、无关敏感日志。
6. 向用户交付修复 commit、summary.yaml 的绝对路径、验证结论和以下提交命令，报告“修复已验证并提交，summary.yaml 待人工检视”。

```bash
af-forge issue summarize --config "$CONFIG" --issue-id <issue-id> \
  --summary-file <summary.yaml的绝对路径> --request-id <本次总结请求ID>
```

此处完成的是本地修复交付，不自动等同于平台 resolved。总结经人工检视且用户明确要求提交后，才执行独立的 issue summarize；不用重新转换文件，也不自行拼 HTTP 请求。若检视导致代码变化，重新验证受影响内容并更新总结；发布重试与请求键规则见 [平台定位参考](references/platform-evidence.md)。不得将测试通过推断为已经合入或平台归档完成。
