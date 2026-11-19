# 复现证据清单

复现前在任务配置启用 `replay.dump: true`。普通 replay 默认 false；这些设置仅作用于本次 Forge replay，不改变平台常驻 client。读取 job 的终态、`report.json` 和 `job.log`；以 report 的 results_dir、runtime、resources、replay_environment 为实际记录，不能仅引用配置期望值。

## 必需信息

- 问题与输入：issue_id、case_key/执行 UUID（若平台提供）、原始完整 seed、复现命令、静态/动态模式、每个失败 shape_index 对应的 Shape、dtype、后端。从平台执行详情和 AFX 保存的 attempt/shape 结果读取；仅有 seed 时不编造输入明细。
- 基线与环境：源码 SHA、是否存在额外改动、基线/候选 deploy job 和 replay job、芯片/设备、Python、torch、torch_npu、AFX、CANN 实际版本、已加载 AF 路径/version/pyautofuse.so SHA-256。来自部署报告、runtime 和执行详情；记录环境一致性警告。
- 失败证据：终态、失败阶段、异常堆栈、失败 Shape、正确性/性能结果、原始日志及 report、results.sqlite 的绝对路径。平台日志有缺失/截断标记时，继续读取远端原始日志。用 AFX 公共结果接口读取数据库，不自行解释表结构。

## 按阶段补充

| 失败阶段 | 需要的定位证据 | 文件入口（具体绝对路径见 evidence.files） |
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

replay 成功或失败结束后，Forge 自动扫描现场并将清单写入现有 `report.json` 的 `evidence`。执行 `af-forge evidence list --config "$CONFIG" --job-id <replay-job-id>` 或读取 status 的 report.json.evidence：

- files：每个实际文件的远端绝对 path、category、size_bytes；文本附 excerpt、excerpt_offset 和 excerpt_truncated。日志取末尾，图/IR/源码取开头。
- directories：实际扫描目录、是否存在、返回文件数。路径不再依赖手工猜测；未执行到相应阶段可能没有文件。
- errors / truncated：读取失败和清单截断状态。最多返回 128 个文件、每文件 2 KiB 文本、总文本 64 KiB。完整原件保留远端，摘录不能替代完整错误或完整 IR。

日志、AF dump、PyTorch debug 和 Inductor cache 都在扫描范围；结果数据库只返回路径与大小，不返回二进制。不会下载所有原件到本机，也不读取 sync.json。对截断的关键 IR/日志使用下方 evidence read 接口继续读取；不能把 excerpt 当作完整文件。任务仍在运行时清单可能尚未生成。

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
