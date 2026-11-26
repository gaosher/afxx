# 复现证据清单

AFXX 使用 AFX 0.4.1+ 的 `afx fuzz replay <seed> --save`，不强制指定数据库。有存活 client 时复用其 home 和队列，无 client 时由 AFX 独立执行；无需本机 seed 历史。统一上传本轮明确关联的 attempt；现有会话关闭上传时返回 UPLOAD_POLICY_CONFLICT，需先启用会话上传。

## 精确查询与归档

AFXX request_id/run 保存队列路径、task ID、attempt ID、结果摘要和日志，不复制共享数据库。结果通过 `afx results list <database> --attempt <id>` 精确读取；task→attempt 尚无公开查询接口，仅在适配层只读查询队列表。队列任务由 client 上传，AFXX 标记 upload.owner=client、state=delegated，不再执行整批补传；delegated 不等于服务端已确认。仅独立执行的 attempt 由 AFXX 通过临时数据库调用 `afx sync once`，结束即清理。

记录原始 seed、issue_id、平台执行 UUID、request_id/run；使用 `replay show --request-id ID --run N` 查询原运行。入队或 task completed 不等于精度通过，需核对全部 backend/shape；CPU not_checked 可为正常参考状态。超时保留原回执，先查询，勿盲目重复提交。

执行环境以 attempt.contexts.environment 为准，environment.json 只是提交端快照。终态下载本次 AFXX 报告及日志，返回 evidence_path；共享数据库和 dump 留在 AFX home，按需读取。

## Dump

`--dump` 或 `replay.dump: true` 传递给 AFX 逐任务开启采集。使用 `evidence list/read --request-id ID --run N` 按需读取远端产物；不自动复制整个共享 dump 目录。新增或发生文件变化的 worker 目录仅标为候选证据，只有诊断中的明确路径才能关联到 attempt；文件种类及保留期限由 AFX 管理，缺少 IR/kernel 时仍需报告证据缺口。

省略 --run 选择最新一次，返回值包含 request_id 和解析后的 run。翻页或续读固定该 run，并使用 next_offset；文件续读可传上次 sha256 检查内容未改变。evidence 命令不接受 --job-id，不创建复跑、不下载整库。
