# 复现证据清单

AFXX replay 统一调用 `afx replay`，通过已有 managed client 执行。单 seed 与 seeds-file 使用同一入口，不创建新的 client HOME，不回退旧执行命令。

## 准备阶段失败

用 `replay show --request-id <ID> --run <N>` 查看原执行。入队回执不等于复现成功；若 seed 尚未执行，报告“准备失败，尚未复现”。AFX 提交没有调用方幂等键，连接中断后先检查已保存的 managed 回执，不盲目重复提交。

## 必需信息

- 原始完整 seed、issue_id、平台执行 UUID、AFXX request_id/run。
- 本次 managed 回执（operation_ids 或 run_id/task_ids）、对应 attempt_id。共享会话里的同 seed 历史记录不能替代本次结果。
- 各后端与 shape_index 的状态、错误阶段、异常堆栈和差异信息，来自 result.json 的 attempts。
- 基线/候选的源码与部署证据；执行身份读取 execution_environments 和 attempt.contexts.environment。submitter_runtime 与 environment.json 是提交端快照，不能证明常驻 worker 已加载候选包。
- 设备、执行与精度策略按实际 AFX task 配置核对，不能假定 AFXX replay.devices/workers 设置生效。

## Dump 能力边界

当前 managed 接口没有逐任务 dump 参数；`--dump` 或 `replay.dump: true` 明确返回 MANAGED_REPLAY_UNSUPPORTED。编译缓存位于 AFX worker 临时目录，回收后可能删除，不等于持久定位证据。

若需要逐 pass MLIR、ASCIR、kernel、tiling 或输入输出张量，应记录缺失并等待相应 AFX 接口；不宣称普通 replay 自动保存了这些文件，不自行重启常驻 client 全局开启 dump，不回退独立 fuzz replay。精度或性能结论必须有本次实际证据。

## Replay 自动返回的证据

`replay show --request-id <ID> --run <N>` 在终态下载本次 AFXX 报告与日志，返回 evidence_path。不会复制整个共享 client 的 results.sqlite、缓存和其他任务产物。

```text
evidence_path/
├── evidence.json       # 文件路径、大小、SHA-256 及跳过原因
├── job/                # 本次 result/status/request/environment.json 与 job.log
└── results/            # 本次 AFXX 结果目录；不保证存在 dump 或独立数据库
```

优先读取 job/result.json 的 attempts、managed、execution_environments 和 managed_specs。CPU 的 not_checked 可以是正常参考状态；其余后端结果需按完整 shape 检查，任务 completed 本身不代表精度通过。

下载校验失败返回 EVIDENCE_DOWNLOAD_FAILED，重查同一次 run 可重试；重复查询复用校验通过的文件。/tmp 不是长期归档。只读取需要的报告字段，缺失证据明确注明，不把不相关的 worker 临时文件归属到当前 seed。
