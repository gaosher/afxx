"""Allocate isolated executions within a workflow group, before remote submission."""
from datetime import datetime, timezone
from pathlib import Path
import uuid
import subprocess
from afxx import common, jobs


def _path(c, request_id, kind):
    common.validate_job_id(request_id)
    return Path(c["local"]["state_dir"]) / (kind + "-groups") / (common.digest(request_id) + ".json")


def _context(c):
    return {"environment": c["connection"]["environment"], "state_dir": c["remote"]["state_dir"]}


def load(c, request_id, kind):
    path = _path(c, request_id, kind)
    common.require_code(path.exists(), kind.upper() + "_NOT_FOUND", "未找到 " + kind + " 分组：" + request_id)
    value = common.read(path)
    common.require_code(value["context"] == _context(c), "CONFIG_CONFLICT",
                        kind + " 分组的远端环境或 state_dir 与当前配置不同")
    return value


def update(c, request_id, run, kind, **fields):
    path = _path(c, request_id, kind)
    with common.lock(path.with_suffix(".lock")):
        group = load(c, request_id, kind)
        group["runs"][run - 1].update(fields)
        common.write(path, group)


def submit(c, request, request_id, dry_run=False, *, kind):
    path = _path(c, request_id, kind)
    with common.lock(path.with_suffix(".lock")):
        group = load(c, request_id, kind) if path.exists() else {
            "request_id": request_id, "context": _context(c), "runs": []}
        run = len(group["runs"]) + 1
        execution = uuid.uuid4().hex
        group["runs"].append({"run": run, "execution": execution,
                              "created_at": datetime.now(timezone.utc).isoformat(),
                              "seeds": request.get("seeds", []), "dry_run": dry_run, "state": "submitting"})
        common.write(path, group)
    # Persist allocation before any upload; a lost connection remains discoverable.
    try:
        data = jobs.submit(c, dict(request, request_id=request_id, run=run), execution, dry_run)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        detail = {"code": getattr(error, "code", "SUBMISSION_FAILED"), "message": str(error)}
        update(c, request_id, run, kind, state="submission_unknown", submission_error=detail)
        raise common.WorkflowError(detail["code"], detail["message"],
                                   {"request_id": request_id, "run": run, "state": "submission_unknown"}) from error
    state = data.get("state", "queued")
    update(c, request_id, run, kind, state=state)
    return dict(data, request_id=request_id, run=run, state=state, dry_run=dry_run)


def select(c, request_id, run, kind):
    group = load(c, request_id, kind)
    number = len(group["runs"]) if run is None else run
    common.require_code(type(number) is int and 1 <= number <= len(group["runs"]),
                        kind.upper() + "_NOT_FOUND", "未找到 " + kind + " run：" + str(number))
    return group["runs"][number - 1]




def status(c, request_id, kind, run=None, *, wait=False, since=None, timeout=30,
           attempt_id=None, fields=None):
    # Resolve latest once, including while waiting; never switch runs mid-poll.
    entry = select(c, request_id, run, kind)
    if entry['dry_run']:
        value = {'state': entry['state'], 'dry_run': True}
    elif wait:
        value = jobs.wait_status(c, entry['execution'], since=since, timeout=timeout, download=False)
    else:
        value = jobs.query_status(c, entry['execution'], attempt_id=attempt_id, fields=fields)
        common.require_code(value['state'] != 'not_found', 'JOB_NOT_FOUND', 'remote run not found')
        if attempt_id is None:
            value['cursor'] = jobs.status_cursor(value)
    return {**{k: v for k, v in value.items() if k not in ('job', 'job_id')},
            'request_id': request_id, 'task_type': kind, 'run': entry['run']}
