# 复现证据清单

定位复现使用 `af-forge replay --dump ...`，覆盖本次任务的 replay.dump 并冻结到 job 配置，不修改原配置。未传 --dump 时沿用配置，普通 replay 默认 false；这些设置仅作用于本次 Forge replay，不改变平台常驻 client。读取 job 的终态、`report.json` 和 `job.log`；以 report 的 results_dir、runtime、resources、replay_environment 为实际记录，不能仅引用配置期望值。

## 必需信息

- 问题与输入：issue_id、case_key/执行 UUID（平台执行记录的标识，若提供；不同于 Forge job_id 和 AFX attempt_id）、原始完整 seed、复现命令、静态/动态模式、每个失败 shape_index 对应的 Shape、dtype、后端。从平台执行详情和 AFX 保存的 attempt/shape 结果读取；仅有 seed 时不编造输入明细。
- 基线与环境：源码 SHA、是否存在额外改动、基线/候选 deploy job 和 replay job、芯片/设备、Python、torch、torch_npu、AFX、CANN 实际版本、已加载 AF 路径/version/pyautofuse.so SHA-256。来自部署报告、runtime 和执行详情；记录环境一致性警告。
- 失败证据：终态、失败阶段、异常堆栈、失败 Shape、正确性/性能结果、原始日志及 report、results.sqlite 的绝对路径。平台日志有缺失/截断标记时，继续读取远端原始日志。用 AFX 公共结果接口读取数据库，不自行解释表结构。

## 按阶段补充

| 失败阶段 | 需要的定位证据 | 文件入口（下载后绝对路径见 evidence.json 的 files[].local_path） |
|---|---|---|
| 图生成/捕获 | seed 解析、生成错误、FX 图或最早可获得的图、输入约束 | job.log、results.sqlite、torch_compile_debug/ |
| 编译 | ASCIR/GE 图、失败 pass 前后 MLIR、生成的 kernel/tiling/host 代码、编译器错误；记录首先失败的位置 | dump/（含 dump/ge/）、torch_compile_debug/、cache/、job.log |
| 运行 | 异常堆栈、首个报错 kernel、对应生成代码、实际输入 Shape；异步报错无法定位时再单独同步执行复现 | job.log、AFX worker 日志、cache/ 中生成代码 |
| 精度 | 失败 shape_index/输出索引、参考与目标结果的差异指标、容差、NaN/Inf 情况；需要逐元素定位时额外采集原输入及参考/目标张量 | results.sqlite 的 attempt/shape 诊断；完整张量需补采集 |
| 性能 | 参考与目标耗时、预热/重复次数、设备条件；正式性能对照关闭 dump，不能以调试运行耗时作为性能通过证据 | results.sqlite 的耗时/telemetry；另做关闭 dump 的对照 |

## 自动产物与边界

- `<results_dir>/dump/`：AF `codegen_compile_debug` 产物，包括实际到达阶段的 ASCIR、逐 pass MLIR 和生成代码；具体文件名随 AF 版本与编译路径变化。
- `<results_dir>/dump/ge/`：GE 图（DUMP_GE_GRAPH=1、DUMP_GRAPH_LEVEL=1），仅经过对应 GE dump 点时生成，不代表任意子图均有文件。
- `<job-dir>/torch_compile_debug/`：PyTorch 编译调试产物；Inductor cache 位于 `<results_dir>/cache/`。检查实际生成路径和日志，不以空目录代替证据。
- `<job-dir>/job.log` 和 AFX worker 日志：包含 Ascend debug 日志；batch 模式同时保留 worker 的原始日志。
- `results.sqlite` 保存 AFX 的结果、诊断和差异摘要，不保证保存完整输入/输出张量。Forge 没有通用张量 dump 开关，精度问题需要时按 AFX 当前支持的采集方式另行取得并记录路径。

开关不保证每个阶段都产生文件：可能尚未执行到该阶段、命中缓存或目标构建不支持。逐项记录“已获取/不适用/缺失及原因”，证据不足以支持根因时先补采集。不要更换 seed、调整容差或改变精度语义制造通过。同步执行、禁用缓存等额外诊断设置按需使用并记入复现命令，不作为默认性能验证条件。

## Replay 自动返回的证据

replay 到达 success/failed 后，本地 `status` 或 `status --wait` 自动将完整 dump、日志、报告、结果数据库和清单下载到 `/tmp/af-forge-evidence-<uid>/<job-id>-<环境标识>/`。返回仅包含 job_id、state、evidence_path 和 failures（attempt_id、seed、backend、shape_indices、stage、简短 error）；失败超过 20 项时附数量和截断标记。异步提交后仍需调用 status/wait，远端不会主动推送文件。重复查询校验并复用本地文件，不重复下载。

本地目录布局：

```text
evidence_path/
├── evidence.json       # 完整文件清单：remote_path、local_path、size_bytes、sha256；skipped 记录跳过原因
├── job/                # report.json、status.json、request.json、依赖报告、job.log 及 job 内调试目录
└── results/            # dump、cache、worker 日志、results.sqlite 等结果目录中的完整普通文件
```

直接读取 evidence_path 下的文件，不需要另执行下载命令。完整清单没有 128 文件或摘录长度上限，包括二进制文件；符号链接和含上传凭据的 sync.json 不复制。报告内 evidence 是带关联信息的摘录，不能用其截断状态判断本地完整清单是否齐全。使用 report 的 associations/diagnostics 明确路径关联 attempt 与文件；unassociated 不代表属于当前失败 Shape。

传输后逐文件校验大小和 SHA-256，完成后才发布 evidence.json。下载失败返回 EVIDENCE_DOWNLOAD_FAILED 和 partial_path，保留未完成现场；重查同一 job 可重试。完整缓存校验通过时不再传输；缺失或被修改则重新下载，旧目录保留为 .previous-*。/tmp 不是长期归档，需长期保留时自行保存。

按失败基础信息挑选所需本地日志、IR、report 字段，避免把全部文件读入 agent 上下文。attempt 详情仍支持 `status --attempt <id> --fields error_stage,error_summary,shape_results`；运行时身份从本地 job/report.json 按键提取。原有 evidence list/read 可用于按需远端查询，但不是复现后的默认取证步骤。

定位顺序：先从 failures 的 stage/error 确定失败阶段，再用 attempt_id 查本地 job/report.json 的 attempts 中 id 相同的记录，按 shape_indices 选择失败 Shape；结合 diagnostics/associations 中明确的远端路径，在 evidence.json 的 files 中匹配 remote_path，读取对应 local_path。attempt_id 是 NPU 上 AFX 为一次执行生成的 UUID，不是平台 issue ID；同一 seed 重跑有新的 attempt。没有明确文件关联时记录未关联，不能由概要推断某个 dump 属于哪个 Shape。
